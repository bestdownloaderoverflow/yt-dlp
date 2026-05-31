package registry

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	neturl "net/url"
	"os/exec"
	"strings"
	"time"

	"gateway-go/utils"

	"golang.org/x/net/proxy"
)

type ProxyRotator struct {
	registry       *ProxyRegistry
	cfg            ProxyRotatorConfig
	dialerFactory  func(socksURL string) (proxy.Dialer, error)
	preRestartHook func(proxyID string)
}

type ProxyRotatorConfig struct {
	HealthCheckTimeoutMs    int
	DrainTimeoutSeconds     int
	DrainPollIntervalMs     int
	RestartStabilizeSeconds int
	IPCheckURL              string
	IPCheckEndpoint         string
}

func NewProxyRotator(reg *ProxyRegistry, cfg ProxyRotatorConfig) *ProxyRotator {
	return &ProxyRotator{
		registry:      reg,
		cfg:           cfg,
		dialerFactory: defaultSOCKS5Dialer,
	}
}

func (pr *ProxyRotator) SetPreRestartHook(hook func(proxyID string)) {
	pr.preRestartHook = hook
}

func defaultSOCKS5Dialer(socksURL string) (proxy.Dialer, error) {
	u, err := neturl.Parse(socksURL)
	if err != nil {
		return nil, fmt.Errorf("invalid SOCKS5 URL %q: %w", socksURL, err)
	}
	host, port, err := net.SplitHostPort(u.Host)
	if err != nil {
		addr := u.Host + ":1080"
		host, port, err = net.SplitHostPort(addr)
		if err != nil {
			return nil, fmt.Errorf("invalid SOCKS5 host %q: %w", u.Host, err)
		}
	}
	dialer, err := proxy.SOCKS5("tcp", net.JoinHostPort(host, port), nil, proxy.Direct)
	if err != nil {
		return nil, fmt.Errorf("SOCKS5 dialer create failed: %w", err)
	}
	return dialer, nil
}

func (pr *ProxyRotator) HealthCheckProxy(proxyID string) (healthy bool, exitIP string) {
	p := pr.registry.GetProxy(proxyID)
	if p == nil {
		return false, ""
	}

	dialer, err := pr.dialerFactory(p.SOCKS5URL)
	if err != nil {
		log.Printf("[proxy:%s] SOCKS5 dialer create error: %v", proxyID, err)
		return false, ""
	}

	timeout := time.Duration(pr.cfg.HealthCheckTimeoutMs) * time.Millisecond
	if timeout < 2000 {
		timeout = 15 * time.Second
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	conn, err := dialSOCKSContext(ctx, dialer, "tcp", "api.ipify.org:443")
	if err != nil {
		log.Printf("[proxy:%s] SOCKS5 tunnel dial error: %v", proxyID, err)
		return false, ""
	}
	_ = conn.Close()

	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return dialSOCKSContext(ctx, dialer, network, addr)
		},
	}
	cl := &http.Client{Transport: transport, Timeout: timeout}

	checkURL := pr.cfg.IPCheckURL
	if checkURL == "" {
		checkURL = "https://api.ipify.org"
	}

	resp, err := cl.Get(checkURL)
	if err != nil {
		log.Printf("[proxy:%s] IP check request error: %v", proxyID, err)
		return false, ""
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[proxy:%s] IP check HTTP %d", proxyID, resp.StatusCode)
		return false, ""
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 128))
	if err != nil {
		log.Printf("[proxy:%s] IP check read error: %v", proxyID, err)
		return false, ""
	}

	ip := strings.TrimSpace(string(body))
	if ip == "" {
		log.Printf("[proxy:%s] IP check returned empty body", proxyID)
		return false, ""
	}

	return true, ip
}

func dialSOCKSContext(ctx context.Context, dialer proxy.Dialer, network, addr string) (net.Conn, error) {
	if contextDialer, ok := dialer.(proxy.ContextDialer); ok {
		return contextDialer.DialContext(ctx, network, addr)
	}
	return dialer.Dial(network, addr)
}

func (pr *ProxyRotator) WaitForProxyDrain(ctx context.Context, proxyID string) bool {
	timeout := time.Duration(pr.cfg.DrainTimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	interval := time.Duration(pr.cfg.DrainPollIntervalMs) * time.Millisecond
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	logInterval := 5 * time.Second
	deadline := time.Now().Add(timeout)
	lastLoggedActive := -1
	lastLogAt := time.Time{}
	for {
		if pr.registry.IsProxyIdle(proxyID) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(interval):
			active := pr.registry.ActiveRequests(proxyID)
			now := time.Now()
			if active != lastLoggedActive || lastLogAt.IsZero() || now.Sub(lastLogAt) >= logInterval {
				log.Printf("[proxy:%s] drain wait: active_requests=%d", proxyID, active)
				lastLoggedActive = active
				lastLogAt = now
			}
		}
	}
}

func (pr *ProxyRotator) RestartProxy(ctx context.Context, proxyID string, reasonCode string) bool {
	startTime := time.Now()

	drained := pr.WaitForProxyDrain(ctx, proxyID)
	if !drained {
		active := pr.registry.ActiveRequests(proxyID)
		log.Printf("[proxy:%s] drain timeout reached; restart postponed (active_requests=%d)", proxyID, active)
		return false
	}

	pr.registry.UpdateRestartState(proxyID, true)
	p := pr.registry.GetProxy(proxyID)
	container := "wireproxy-01"
	if p != nil && p.Container != "" {
		container = p.Container
	}

	if pr.preRestartHook != nil {
		pr.preRestartHook(proxyID)
	}

	oldIP := ""
	if p != nil {
		oldIP = p.LastExitIP
	}

	log.Printf("[proxy:%s] restarting %s", proxyID, container)
	if err := pr.restartContainer(ctx, container); err != nil {
		log.Printf("[proxy:%s] restart error: %v", proxyID, err)
		pr.registry.MarkRestarted(proxyID, false, "")
		pr.recordProxyRestartLog(proxyID, false, startTime, reasonCode)
		return false
	}

	select {
	case <-ctx.Done():
		pr.registry.MarkRestarted(proxyID, false, "")
		pr.recordProxyRestartLog(proxyID, false, startTime, reasonCode)
		return false
	case <-time.After(5 * time.Second):
	}

	if pr.preRestartHook != nil {
		pr.preRestartHook(proxyID)
	}

	healthy := false
	exitIP := ""
	for i := 0; i < 6; i++ {
		h, ip := pr.HealthCheckProxy(proxyID)
		if h {
			healthy = true
			exitIP = ip
			break
		}
		select {
		case <-ctx.Done():
			pr.registry.MarkRestarted(proxyID, false, "")
			pr.recordProxyRestartLog(proxyID, false, startTime, reasonCode)
			return false
		case <-time.After(5 * time.Second):
		}
	}

	if healthy {
		if oldIP != "" && exitIP != "" && exitIP == oldIP {
			log.Printf("[proxy:%s] exit IP unchanged after restart (%s); considering retry", proxyID, exitIP)
		}
		stabilize := time.Duration(pr.cfg.RestartStabilizeSeconds) * time.Second
		if stabilize < 0 {
			stabilize = 0
		}
		select {
		case <-ctx.Done():
			pr.registry.MarkRestarted(proxyID, false, "")
			pr.recordProxyRestartLog(proxyID, false, startTime, reasonCode)
			return false
		case <-time.After(stabilize):
		}
		pr.registry.MarkRestarted(proxyID, true, exitIP)
		pr.recordProxyRestartLog(proxyID, true, startTime, reasonCode)
		return true
	}

	log.Printf("[proxy:%s] health check failed after restart", proxyID)
	pr.registry.MarkRestarted(proxyID, false, "")
	pr.recordProxyRestartLog(proxyID, false, startTime, reasonCode)
	return false
}

func (pr *ProxyRotator) restartContainer(ctx context.Context, container string) error {
	cmd := exec.CommandContext(ctx, "docker", "restart", "--time", "30", container)
	out, err := cmd.CombinedOutput()
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("docker restart %s failed: %v (%s)", container, err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (pr *ProxyRotator) recordProxyRestartLog(proxyID string, success bool, startTime time.Time, reasonCode string) {
	if strings.TrimSpace(reasonCode) == "" {
		reasonCode = "unknown"
	}
	duration := time.Since(startTime)
	durationMs := duration.Milliseconds()

	if success {
		log.Printf("[proxy:%s] restart complete: success=true duration_ms=%d", proxyID, durationMs)
	} else {
		log.Printf("[proxy:%s] restart failed: success=false duration_ms=%d", proxyID, durationMs)
	}

	p := pr.registry.GetProxy(proxyID)
	if p == nil {
		return
	}
	entry := utils.RestartLogEntry{
		WorkerID:        proxyID,
		Success:         success,
		DurationMs:      durationMs,
		ProbeFailures:   p.ProbeFailures,
		RestartFailures: p.RestartFailures,
		ReasonCode:      reasonCode,
	}
	_ = utils.LogRestart(entry)
}
