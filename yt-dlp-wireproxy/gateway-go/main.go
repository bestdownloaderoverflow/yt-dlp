package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"gateway-go/delivery"
	"gateway-go/handlers"
	"gateway-go/metrics"
	"gateway-go/registry"
	"gateway-go/utils"
)

func main() {
	cfg := utils.LoadConfig()

	// Initialize Prometheus metrics registry
	metrics.Register()

	// Initialize Redis-backed restart log (best-effort, non-fatal if Redis unavailable)
	if err := utils.InitRedis(cfg.RedisURL); err != nil {
		log.Printf("WARNING: restart log Redis unavailable: %v", err)
	}

	// Base HTTP client for upstream media requests.
	baseTransport := &http.Transport{
		MaxIdleConns:        512,
		MaxIdleConnsPerHost: 128,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  false,
	}
	client := &http.Client{Transport: baseTransport}

	// Worker registry and VPN rotator.
	reg := registry.NewWorkerRegistry(registry.Config{
		WorkerCount:               cfg.WorkerCount,
		RateLimitCooldownSeconds:  cfg.RateLimitCooldownSeconds,
		DegradedTimeoutSeconds:    cfg.DegradedTimeoutSeconds,
		RestartBackoffBase:        cfg.RestartBackoffBase,
		RestartBackoffMax:         cfg.RestartBackoffMax,
		RestartBackoffJitter:      cfg.RestartBackoffJitter,
		RestartBudgetLimit:        cfg.RestartBudgetLimit,
		RestartBudgetWindow:       cfg.RestartBudgetWindow,
		RestartQuarantineSeconds:  cfg.RestartQuarantineSeconds,
		HealthFailureThreshold:    cfg.HealthFailureThreshold,
		UnhealthyRestartThreshold: cfg.UnhealthyRestartThreshold,
		WorkerCountries:           cfg.WorkerCountries,
	})
	rotator := registry.NewVPNRotator(reg, registry.RotatorConfig{
		HealthCheckTimeoutMs:    cfg.HealthCheckTimeoutMs,
		GluetunPassword:         cfg.GluetunPassword,
		DrainTimeoutSeconds:     cfg.DrainTimeoutSeconds,
		DrainPollIntervalMs:     cfg.DrainPollIntervalMs,
		RestartStabilizeSeconds: cfg.RestartStabilizeSeconds,
	})

	// Proxy registry and rotator (wireproxy-based).
	proxyReg := registry.NewProxyRegistry(registry.ProxyConfig{
		ProxyCount:               cfg.ProxyCount,
		ProxyCooldownSeconds:     cfg.ProxyCooldownSeconds,
		ProxyCountriess:          cfg.ProxyCountries,
		HealthFailureThreshold:   cfg.ProxyHealthFailureThreshold,
		RestartBackoffBase:       cfg.RestartBackoffBase,
		RestartBackoffMax:        cfg.RestartBackoffMax,
		RestartBackoffJitter:     cfg.RestartBackoffJitter,
		RestartQuarantineSeconds: cfg.RestartQuarantineSeconds,
		RestartBudgetLimit:       cfg.RestartBudgetLimit,
		RestartBudgetWindow:      cfg.RestartBudgetWindow,
	})
	proxyRot := registry.NewProxyRotator(proxyReg, registry.ProxyRotatorConfig{
		HealthCheckTimeoutMs:    cfg.HealthCheckTimeoutMs,
		DrainTimeoutSeconds:     cfg.DrainTimeoutSeconds,
		DrainPollIntervalMs:     cfg.DrainPollIntervalMs,
		RestartStabilizeSeconds: cfg.RestartStabilizeSeconds,
		IPCheckURL:              cfg.IPCheckURL,
		IPCheckEndpoint:         cfg.IPCheckEndpoint,
	})

	// Extractor pool (IPC to Python workers).
	var ext *ExtractorPool
	if cfg.ExtractorIPCEnabled {
		timeout := time.Duration(cfg.ExtractorTimeoutMs) * time.Millisecond
		pool, err := NewExtractorPool(cfg.WorkerCount, timeout)
		if err != nil {
			log.Fatalf("failed to initialize extractor IPC pool: %v", err)
		}
		ext = pool
		log.Printf(
			"extractor IPC enabled: workers=%d read_timeout_ms=%d connect_timeout_ms=%d",
			cfg.WorkerCount,
			cfg.ExtractorTimeoutMs,
			10000,
		)
	}

	// Delivery engine (streaming orchestration in Go).
	del := delivery.New(delivery.DeliveryConfig{
		DegradedRetryAfter: cfg.DegradedRetryAfter,
	}, client, deliveryWorkerLookup{reg})

	// Purge stale proxy connections before each container restart so that
	// the old VPN network namespace's TCP connections are not reused.
	rotator.SetPreRestartHook(func(wid string) { del.PurgeWorkerClient(wid) })

	// SOCKS5 proxy client cache purge hook.
	proxyRot.SetPreRestartHook(func(proxyID string) {
		p := proxyReg.GetProxy(proxyID)
		if p != nil {
			del.PurgeSOCKS5Client(p.SOCKS5URL)
		}
	})

	// Handlers serve all HTTP routes.
	h := handlers.New(cfg, reg, rotator, proxyReg, proxyRot, wrapExtractor(ext), del, client)
	defer h.Shutdown()

	go h.RestartScheduler()
	go h.UptimeChecker()
	go h.HealthMonitor()
	go h.ProxyHealthMonitor()
	go h.ProxyRestartScheduler()

	addr := fmt.Sprintf(":%d", cfg.GatewayPort)
	server := &http.Server{Addr: addr, Handler: h}
	log.Printf("starting Go gateway on port %d", cfg.GatewayPort)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("gateway failed: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down gateway...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown error: %v", err)
	}
}

// deliveryWorkerLookup bridges delivery.WorkerLookup to the registry.
type deliveryWorkerLookup struct{ Registry *registry.WorkerRegistry }

func (d deliveryWorkerLookup) GetWorker(id string) *delivery.Worker {
	w := d.Registry.GetWorker(id)
	if w == nil {
		return nil
	}
	return &delivery.Worker{Host: w.Host, ProxyPort: 0}
}

// wrapExtractor adapts *ExtractorPool to handlers.Extractor interface.
func wrapExtractor(pool *ExtractorPool) handlers.Extractor {
	if pool == nil {
		return nil
	}
	return &extractorAdapter{pool: pool}
}

type extractorAdapter struct{ pool *ExtractorPool }

func (e *extractorAdapter) ExtractInfo(wid, url, proxy, impersonate string) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.ExtractInfo(wid, url, proxy, impersonate)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) Fetch(wid, url, proxy, impersonate string, forceIPv6 bool) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.Fetch(wid, url, proxy, impersonate, forceIPv6)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) TikTok(wid, url, proxy, impersonate string) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.TikTok(wid, url, proxy, impersonate)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) TikTokDownloadPrepare(wid, key string, download bool) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.TikTokDownloadPrepare(wid, key, download)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) TikTokDownloadRefresh(wid, key string, download bool) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.TikTokDownloadRefresh(wid, key, download)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) DownloadPrepare(wid, key string, download bool) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.DownloadPrepare(wid, key, download)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) DownloadRefresh(wid, key string, download bool) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.DownloadRefresh(wid, key, download)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) ResolveFormats(wid, url, format, proxy, impersonate string) (map[string]any, *handlers.IPCError, error) {
	r, err, network := e.pool.ResolveFormats(wid, url, format, proxy, impersonate)
	return r, wrapIPCErr(err), network
}
func (e *extractorAdapter) Health(wid string) error            { return e.pool.Health(wid) }
func (e *extractorAdapter) PickWorker(preferred string) string { return e.pool.PickWorker(preferred) }
func (e *extractorAdapter) HasWorker(wid string) bool          { return e.pool.HasWorker(wid) }
func (e *extractorAdapter) InFlight(wid string) int            { return e.pool.InFlight(wid) }

func wrapIPCErr(err *ipcError) *handlers.IPCError {
	if err == nil {
		return nil
	}
	return &handlers.IPCError{Code: err.Code, Message: err.Message, Status: err.Status}
}
