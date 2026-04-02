package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const uptimeRestartInterval = 24 * time.Hour
const (
	videoChunkSizeBytes = 10 * 1024 * 1024
	audioChunkSizeBytes = 8 * 1024 * 1024
)

type Config struct {
	GatewayPort               int
	WorkerCount               int
	ExtractorIPCEnabled       bool
	ExtractorPythonBin        string
	ExtractorWorkerPath       string
	ExtractorTimeoutMs        int
	GluetunPassword           string
	MaxRetries                int
	HealthCheckTimeoutMs      int
	HealthMonitorIntervalMs   int
	HealthFailureThreshold    int
	RateLimitCooldownSeconds  int
	RestartBackoffBase        int
	RestartBackoffMax         int
	RestartBudgetLimit        int
	RestartBudgetWindow       int
	RestartQuarantineSeconds  int
	RestartBackoffJitter      int
	DegradedRetryAfter        int
	GatewayRLWindowSeconds    int
	GatewayRLFetchLimit       int
	GatewayRLDownloadLimit    int
	DrainTimeoutSeconds       int
	DrainPollIntervalMs       int
	RestartStabilizeSeconds   int
	UnhealthyRestartThreshold int
}

type Worker struct {
	ID               string
	Host             string
	APIPort          int
	ProxyPort        int
	ControlPort      int
	Healthy          bool
	Restarting       bool
	RestartScheduled bool
	Failures         int
	RestartFailures  int
	ProbeFailures    int
	StartedAt        time.Time
	ActiveRequests   int
	LastRateLimit    time.Time
	NextRestartAt    time.Time
	QuarantineUntil  time.Time
	RestartEvents    []time.Time
	BreakerOpen      bool
}

func (w *Worker) APIURL() string {
	return fmt.Sprintf("http://%s:%d", w.Host, w.APIPort)
}

func (w *Worker) ControlURL(password string) string {
	return fmt.Sprintf("http://admin:%s@%s:%d", password, w.Host, w.ControlPort)
}

func (w *Worker) HTTPProxyURL() string {
	return fmt.Sprintf("http://%s:%d", w.Host, w.ProxyPort)
}

type WorkerRegistry struct {
	mu      sync.Mutex
	workers []*Worker
	cfg     Config
}

func NewWorkerRegistry(cfg Config) *WorkerRegistry {
	now := time.Now()
	workers := make([]*Worker, 0, cfg.WorkerCount)
	for i := 1; i <= cfg.WorkerCount; i++ {
		workers = append(workers, &Worker{
			ID:          fmt.Sprintf("w%d", i),
			Host:        fmt.Sprintf("gluetun-%d", i),
			APIPort:     9487,
			ProxyPort:   8888,
			ControlPort: 8000,
			Healthy:     false,
			StartedAt:   now,
		})
	}
	log.Printf("initialized %d workers", cfg.WorkerCount)
	return &WorkerRegistry{workers: workers, cfg: cfg}
}

func (r *WorkerRegistry) getWorkerUnlocked(workerID string) *Worker {
	for _, w := range r.workers {
		if w.ID == workerID {
			return w
		}
	}
	return nil
}

func (r *WorkerRegistry) GetWorker(workerID string) *Worker {
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return nil
	}
	copyW := *w
	return &copyW
}

func (r *WorkerRegistry) GetHealthyWorkers(exclude map[string]bool) []*Worker {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	result := make([]*Worker, 0, len(r.workers))
	for _, w := range r.workers {
		if now.Before(w.QuarantineUntil) {
			continue
		}
		if !w.Healthy || w.Restarting || w.RestartScheduled || w.BreakerOpen {
			continue
		}
		if exclude != nil && exclude[w.ID] {
			continue
		}
		if !w.LastRateLimit.IsZero() && now.Sub(w.LastRateLimit) <= time.Duration(r.cfg.RateLimitCooldownSeconds)*time.Second {
			continue
		}
		copyW := *w
		result = append(result, &copyW)
	}
	return result
}

func (r *WorkerRegistry) MarkRateLimited(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.LastRateLimit = time.Now()
		w.Failures++
		log.Printf("[%s] marked as rate limited", workerID)
	}
}

func (r *WorkerRegistry) MarkFailure(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.Failures++
		log.Printf("[%s] marked as failed", workerID)
	}
}

func (r *WorkerRegistry) ScheduleRestart(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		if !w.Restarting && !w.RestartScheduled {
			w.RestartScheduled = true
			w.BreakerOpen = true
			log.Printf("[%s] restart scheduled", workerID)
			return true
		}
	}
	return false
}

func (r *WorkerRegistry) CanStartRestart(workerID string) (bool, string, int) {
	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return false, "unknown_worker", 0
	}
	now := time.Now()
	if w.Restarting {
		return false, "already_restarting", 0
	}
	if now.Before(w.QuarantineUntil) {
		return false, "quarantine", int(time.Until(w.QuarantineUntil).Seconds())
	}
	if now.Before(w.NextRestartAt) {
		return false, "backoff", int(time.Until(w.NextRestartAt).Seconds())
	}
	return true, "ok", 0
}

func (r *WorkerRegistry) UpdateRestartState(workerID string, restarting bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.Restarting = restarting
		if restarting {
			w.Healthy = false
		}
	}
}

func (r *WorkerRegistry) MarkRestarted(workerID string, success bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return
	}

	w.Restarting = false
	w.RestartScheduled = false
	if success {
		w.Healthy = true
		w.Failures = 0
		w.RestartFailures = 0
		w.StartedAt = time.Now()
		w.LastRateLimit = time.Time{}
		w.NextRestartAt = time.Time{}
		w.QuarantineUntil = time.Time{}
		w.RestartEvents = nil
		w.BreakerOpen = false
		log.Printf("[%s] restarted successfully", workerID)
		return
	}

	now := time.Now()
	w.Healthy = false
	w.Failures++
	w.RestartFailures++

	exp := w.RestartFailures - 1
	if exp < 0 {
		exp = 0
	}
	if exp > 6 {
		exp = 6
	}
	delay := r.cfg.RestartBackoffBase * (1 << exp)
	if delay > r.cfg.RestartBackoffMax {
		delay = r.cfg.RestartBackoffMax
	}
	if r.cfg.RestartBackoffJitter > 0 {
		delay += rand.Intn(r.cfg.RestartBackoffJitter + 1)
	}
	w.NextRestartAt = now.Add(time.Duration(delay) * time.Second)

	windowStart := now.Add(-time.Duration(r.cfg.RestartBudgetWindow) * time.Second)
	filtered := w.RestartEvents[:0]
	for _, t := range w.RestartEvents {
		if t.After(windowStart) {
			filtered = append(filtered, t)
		}
	}
	w.RestartEvents = append(filtered, now)
	if len(w.RestartEvents) >= r.cfg.RestartBudgetLimit {
		w.QuarantineUntil = now.Add(time.Duration(r.cfg.RestartQuarantineSeconds) * time.Second)
		w.NextRestartAt = w.QuarantineUntil
		log.Printf("[%s] entering quarantine for %ds after %d restart failures", workerID, r.cfg.RestartQuarantineSeconds, len(w.RestartEvents))
	}

	w.RestartScheduled = true
	w.BreakerOpen = true
	log.Printf("[%s] restart failed; retry after %ds", workerID, delay)
}

func (r *WorkerRegistry) OpenCircuit(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		if !w.BreakerOpen {
			log.Printf("[%s] circuit opened (drain mode)", workerID)
		}
		w.BreakerOpen = true
	}
}

func (r *WorkerRegistry) CloseCircuit(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.BreakerOpen = false
	}
}

func (r *WorkerRegistry) SetHealthy(workerID string, healthy bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		if w.Healthy != healthy {
			if healthy {
				log.Printf("[%s] marked healthy after successful probe", workerID)
			} else {
				log.Printf("[%s] marked unhealthy after failed probe", workerID)
			}
		}
		w.Healthy = healthy
		if healthy {
			w.BreakerOpen = false
		}
	}
}

func (r *WorkerRegistry) RecordProbe(workerID string, healthy bool) {
	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return
	}

	threshold := r.cfg.HealthFailureThreshold
	if threshold < 1 {
		threshold = 1
	}

	if healthy {
		w.ProbeFailures = 0
		if !w.Healthy {
			log.Printf("[%s] marked healthy after successful probe", workerID)
		}
		w.Healthy = true
		w.BreakerOpen = false
		return
	}

	w.ProbeFailures++
	if w.ProbeFailures >= threshold {
		if w.Healthy {
			log.Printf("[%s] marked unhealthy after %d consecutive failed probes", workerID, w.ProbeFailures)
		}
		w.Healthy = false
	}
}

func (r *WorkerRegistry) GetWorkersNeedingRestart() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	needing := []string{}
	now := time.Now()
	for _, w := range r.workers {
		uptime := now.Sub(w.StartedAt)
		if uptime >= uptimeRestartInterval && w.Healthy && !w.Restarting && !w.RestartScheduled {
			needing = append(needing, w.ID)
		}
	}
	return needing
}

func (r *WorkerRegistry) IsWorkerIdle(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		return w.ActiveRequests == 0
	}
	return false
}

func (r *WorkerRegistry) ActiveRequests(workerID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		return w.ActiveRequests
	}
	return 0
}

func (r *WorkerRegistry) IncrementActive(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.ActiveRequests++
	}
}

func (r *WorkerRegistry) DecrementActive(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil && w.ActiveRequests > 0 {
		w.ActiveRequests--
	}
}

func (r *WorkerRegistry) ShouldRestartUnhealthy(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return false
	}

	threshold := r.cfg.UnhealthyRestartThreshold
	if threshold < 1 {
		return false
	}
	if w.Healthy || w.Restarting || w.RestartScheduled {
		return false
	}
	if w.ActiveRequests > 0 {
		return false
	}

	now := time.Now()
	if now.Before(w.QuarantineUntil) || now.Before(w.NextRestartAt) {
		return false
	}

	return w.ProbeFailures >= threshold
}

func (r *WorkerRegistry) WorkersSnapshot() []Worker {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Worker, 0, len(r.workers))
	for _, w := range r.workers {
		out = append(out, *w)
	}
	return out
}

type VPNRotator struct {
	registry *WorkerRegistry
	cfg      Config
	healthCl *http.Client
}

func NewVPNRotator(registry *WorkerRegistry, cfg Config) *VPNRotator {
	timeout := cfg.HealthCheckTimeoutMs
	if timeout < 1000 {
		timeout = 1000
	}
	return &VPNRotator{
		registry: registry,
		cfg:      cfg,
		healthCl: &http.Client{Timeout: time.Duration(timeout) * time.Millisecond},
	}
}

func (v *VPNRotator) restartContainer(container string) error {
	cmd := exec.Command("docker", "restart", "--time", "30", container)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker restart %s failed: %v (%s)", container, err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (v *VPNRotator) RestartWorker(ctx context.Context, workerID string) bool {
	drained := v.waitForDrain(ctx, workerID)
	if !drained {
		log.Printf("[%s] drain timeout reached; forcing container restart", workerID)
	}

	v.registry.UpdateRestartState(workerID, true)
	gluetun := fmt.Sprintf("ytdlp-gluetun-%s", strings.TrimPrefix(workerID, "w"))
	ytdlp := fmt.Sprintf("ytdlp-stream-%s", strings.TrimPrefix(workerID, "w"))

	log.Printf("[%s] restarting %s", workerID, gluetun)
	if err := v.restartContainer(gluetun); err != nil {
		log.Printf("[%s] restart error: %v", workerID, err)
		v.registry.MarkRestarted(workerID, false)
		return false
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(10 * time.Second):
	}

	log.Printf("[%s] restarting %s", workerID, ytdlp)
	if err := v.restartContainer(ytdlp); err != nil {
		log.Printf("[%s] restart error: %v", workerID, err)
		v.registry.MarkRestarted(workerID, false)
		return false
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(5 * time.Second):
	}

	healthy := false
	for i := 0; i < 6; i++ {
		if v.healthCheck(workerID) {
			healthy = true
			break
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(5 * time.Second):
		}
	}

	if healthy {
		stabilize := time.Duration(v.cfg.RestartStabilizeSeconds) * time.Second
		if stabilize < 0 {
			stabilize = 0
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(stabilize):
		}
		v.registry.MarkRestarted(workerID, true)
		return true
	}

	log.Printf("[%s] health check failed after restart", workerID)
	v.registry.MarkRestarted(workerID, false)
	return false
}

func (v *VPNRotator) waitForDrain(ctx context.Context, workerID string) bool {
	timeout := time.Duration(v.cfg.DrainTimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	interval := time.Duration(v.cfg.DrainPollIntervalMs) * time.Millisecond
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	logInterval := 5 * time.Second
	deadline := time.Now().Add(timeout)
	lastLoggedActive := -1
	lastLogAt := time.Time{}
	for {
		if v.registry.IsWorkerIdle(workerID) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(interval):
			active := v.registry.ActiveRequests(workerID)
			now := time.Now()
			if active != lastLoggedActive || lastLogAt.IsZero() || now.Sub(lastLogAt) >= logInterval {
				log.Printf("[%s] drain wait: active_requests=%d", workerID, active)
				lastLoggedActive = active
				lastLogAt = now
			}
		}
	}
}

func (v *VPNRotator) healthCheck(workerID string) bool {
	worker := v.registry.GetWorker(workerID)
	if worker == nil {
		return false
	}

	ipURL := fmt.Sprintf("%s/v1/publicip/ip", worker.ControlURL(v.cfg.GluetunPassword))
	if req, err := http.NewRequest(http.MethodGet, ipURL, nil); err == nil {
		resp, err := v.healthCl.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
		}
	}

	addr := fmt.Sprintf("%s:%d", worker.Host, worker.APIPort)
	timeout := v.healthCl.Timeout
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		log.Printf("[%s] worker TCP health check error: %v", workerID, err)
		return false
	}
	_ = conn.Close()
	return true
}

type SlidingWindowRateLimiter struct {
	mu            sync.Mutex
	limit         int
	windowSeconds int
	events        map[string][]time.Time
}

func NewSlidingWindowRateLimiter(limit, windowSeconds int) *SlidingWindowRateLimiter {
	if limit < 1 {
		limit = 1
	}
	if windowSeconds < 1 {
		windowSeconds = 1
	}
	return &SlidingWindowRateLimiter{limit: limit, windowSeconds: windowSeconds, events: map[string][]time.Time{}}
}

func (l *SlidingWindowRateLimiter) Check(key string) (bool, int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	cutoff := now.Add(-time.Duration(l.windowSeconds) * time.Second)
	q := l.events[key]
	idx := 0
	for idx < len(q) && (q[idx].Before(cutoff) || q[idx].Equal(cutoff)) {
		idx++
	}
	if idx > 0 {
		q = q[idx:]
	}
	if len(q) >= l.limit {
		retryAfter := int(q[0].Add(time.Duration(l.windowSeconds) * time.Second).Sub(now).Seconds())
		if retryAfter < 1 {
			retryAfter = 1
		}
		l.events[key] = q
		return false, retryAfter
	}
	q = append(q, now)
	l.events[key] = q
	return true, 0
}

type proxyResult struct {
	success       bool
	isRateLimit   bool
	shouldRestart bool
	status        int
	wroteDirect   bool
}

func isClientAbortError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, net.ErrClosed) {
		return true
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "broken pipe") ||
		strings.Contains(msg, "connection reset by peer") ||
		strings.Contains(msg, "context canceled")
}

type Gateway struct {
	cfg            Config
	registry       *WorkerRegistry
	rotator        *VPNRotator
	client         *http.Client
	extractor      *ExtractorPool
	rlFetch        *SlidingWindowRateLimiter
	rlDownload     *SlidingWindowRateLimiter
	restartTasksMu sync.Mutex
	restartTasks   map[string]context.CancelFunc
	ctx            context.Context
	cancel         context.CancelFunc
}

func NewGateway(cfg Config) *Gateway {
	tr := &http.Transport{
		MaxIdleConns:        512,
		MaxIdleConnsPerHost: 128,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  false,
	}
	ctx, cancel := context.WithCancel(context.Background())
	registry := NewWorkerRegistry(cfg)
	gw := &Gateway{
		cfg:          cfg,
		registry:     registry,
		rotator:      NewVPNRotator(registry, cfg),
		client:       &http.Client{Transport: tr},
		rlFetch:      NewSlidingWindowRateLimiter(cfg.GatewayRLFetchLimit, cfg.GatewayRLWindowSeconds),
		rlDownload:   NewSlidingWindowRateLimiter(cfg.GatewayRLDownloadLimit, cfg.GatewayRLWindowSeconds),
		restartTasks: map[string]context.CancelFunc{},
		ctx:          ctx,
		cancel:       cancel,
	}
	if cfg.ExtractorIPCEnabled {
		workerPath := resolveWorkerScriptPath(cfg.ExtractorWorkerPath)
		timeout := time.Duration(cfg.ExtractorTimeoutMs) * time.Millisecond
		pool, err := NewExtractorPool(cfg.WorkerCount, cfg.ExtractorPythonBin, workerPath, timeout)
		if err != nil {
			log.Fatalf("failed to initialize extractor IPC pool: %v", err)
		}
		gw.extractor = pool
		log.Printf("extractor IPC enabled: python=%s script=%s workers=%d", cfg.ExtractorPythonBin, workerPath, cfg.WorkerCount)
	}
	return gw
}

func (g *Gateway) ipcReady() bool {
	return g.cfg.ExtractorIPCEnabled && g.extractor != nil
}

func (g *Gateway) requireIPCReady(w http.ResponseWriter) bool {
	if g.ipcReady() {
		return true
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Extractor IPC unavailable",
		"detail": "EXTRACTOR_IPC_ENABLED=true but extractor workers are not ready",
	})
	return false
}

func (g *Gateway) mediaHTTPClient(workerID string) *http.Client {
	if workerID == "" {
		return g.client
	}
	worker := g.registry.GetWorker(workerID)
	if worker == nil || worker.Host == "" || worker.ProxyPort <= 0 {
		return g.client
	}
	baseTransport, ok := g.client.Transport.(*http.Transport)
	if !ok || baseTransport == nil {
		return g.client
	}
	proxyURL, err := url.Parse(worker.HTTPProxyURL())
	if err != nil {
		log.Printf("[%s] invalid worker proxy URL: %v", workerID, err)
		return g.client
	}
	tr := baseTransport.Clone()
	tr.Proxy = http.ProxyURL(proxyURL)
	return &http.Client{
		Transport: tr,
		Timeout:   g.client.Timeout,
	}
}

func shouldBypassWorkerProxy(plan map[string]any) bool {
	sessionType, _ := plan["session_type"].(string)
	switch sessionType {
	case "photo", "slideshow":
		return true
	}
	mediaType, _ := plan["media_type"].(string)
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(mediaType)), "image/") {
		return true
	}
	return len(anyToStringSlice(plan["photo_urls"])) > 0
}

func (g *Gateway) mediaHTTPClientForPlan(workerID string, plan map[string]any) *http.Client {
	if shouldBypassWorkerProxy(plan) {
		return g.client
	}
	return g.mediaHTTPClient(workerID)
}

func (g *Gateway) Shutdown() {
	g.cancel()
	if g.extractor != nil {
		g.extractor.Close()
	}
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")
		w.WriteHeader(http.StatusNoContent)
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", "*")

	switch r.URL.Path {
	case "/":
		g.handleRoot(w, r)
	case "/fetch":
		g.handleFetch(w, r)
	case "/download":
		g.handleDownload(w, r)
	case "/info":
		g.handleInfo(w, r)
	case "/stream/video", "/stream/video-chunked", "/stream/mp3", "/stream/mp3-chunked", "/stream/m4a":
		g.handleStream(w, r)
	case "/tiktok":
		g.handleTikTok(w, r)
	case "/tiktok/download":
		g.handleTikTokDownload(w, r)
	case "/health":
		g.handleHealth(w, r)
	case "/tunnel":
		g.handleTunnel(w, r)
	default:
		http.NotFound(w, r)
	}
}

func copyHeader(dst, src http.Header, skip map[string]bool) {
	for k, values := range src {
		if skip[strings.ToLower(k)] {
			continue
		}
		for _, v := range values {
			dst.Add(k, v)
		}
	}
}

func (g *Gateway) buildForwardHeaders(r *http.Request, method string) http.Header {
	h := make(http.Header)
	for _, key := range []string{"Accept", "Accept-Language", "User-Agent", "Range", "If-Range"} {
		if v := r.Header.Get(key); v != "" {
			h.Set(key, v)
		}
	}

	incomingXFF := r.Header.Get("X-Forwarded-For")
	remoteIP := r.RemoteAddr
	if i := strings.LastIndex(remoteIP, ":"); i != -1 {
		remoteIP = remoteIP[:i]
	}
	if incomingXFF != "" && remoteIP != "" {
		h.Set("X-Forwarded-For", incomingXFF+", "+remoteIP)
	} else if incomingXFF != "" {
		h.Set("X-Forwarded-For", incomingXFF)
	} else if remoteIP != "" {
		h.Set("X-Forwarded-For", remoteIP)
	}

	proto := "http"
	if r.TLS != nil {
		proto = "https"
	}
	h.Set("X-Forwarded-Proto", proto)

	if method == http.MethodPost {
		ct := r.Header.Get("Content-Type")
		if ct == "" {
			ct = "application/json"
		}
		h.Set("Content-Type", ct)
	}

	return h
}

func (g *Gateway) handleRoot(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"service":               "yt-dlp gateway",
		"transport":             "go-http + python-ipc",
		"extractor_ipc_enabled": g.cfg.ExtractorIPCEnabled,
		"extractor_ipc_ready":   g.ipcReady(),
		"status":                "ok",
	})
}

func (g *Gateway) handleFetch(w http.ResponseWriter, r *http.Request) {
	if !g.checkRateLimit(w, r, g.rlFetch, "fetch") {
		return
	}
	if g.cfg.ExtractorIPCEnabled {
		if !g.requireIPCReady(w) {
			return
		}
		g.handleFetchViaExtractorIPC(w, r)
		return
	}
	g.proxyWithRetry(w, r, "/fetch", http.MethodGet, "", false)
}

func (g *Gateway) handleFetchViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("url"))
	if url == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing url query parameter"})
		return
	}
	proxy := strings.TrimSpace(r.URL.Query().Get("proxy"))
	impersonate := strings.TrimSpace(r.URL.Query().Get("impersonate"))

	preferred := extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount)
	workerID := g.selectExtractorWorker(preferred, true)
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}

	result, rpcErr, err := g.extractor.Fetch(workerID, url, proxy, impersonate)
	if err != nil {
		log.Printf("[extractor:%s] fetch ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{
			"error":  rpcErr.Code,
			"detail": rpcErr.Message,
		})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (g *Gateway) handleDownload(w http.ResponseWriter, r *http.Request) {
	if !g.checkRateLimit(w, r, g.rlDownload, "download") {
		return
	}
	if g.cfg.ExtractorIPCEnabled {
		if !g.requireIPCReady(w) {
			return
		}
		g.handleDownloadViaExtractorIPC(w, r)
		return
	}
	// Download keys are worker-affine (`wX::...`), so keep routing strict to owner.
	g.proxyWithRetry(w, r, "/download", http.MethodGet, extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount), true)
}

func (g *Gateway) handleInfo(w http.ResponseWriter, r *http.Request) {
	if g.cfg.ExtractorIPCEnabled {
		if !g.requireIPCReady(w) {
			return
		}
		g.handleInfoViaExtractorIPC(w, r)
		return
	}
	g.proxyWithRetry(w, r, "/info", http.MethodGet, "", false)
}

func (g *Gateway) handleInfoViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("url"))
	if url == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing url query parameter"})
		return
	}
	proxy := strings.TrimSpace(r.URL.Query().Get("proxy"))
	impersonate := strings.TrimSpace(r.URL.Query().Get("impersonate"))

	preferred := extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount)
	workerID := g.selectExtractorWorker(preferred, false)
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}

	result, rpcErr, err := g.extractor.ExtractInfo(workerID, url, proxy, impersonate)
	if err != nil {
		log.Printf("[extractor:%s] ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{
			"error":  rpcErr.Code,
			"detail": rpcErr.Message,
		})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (g *Gateway) selectExtractorWorker(preferred string, requireHealthy bool) string {
	if g.extractor == nil {
		return ""
	}
	if preferred != "" && g.extractor.HasWorker(preferred) {
		if !requireHealthy {
			return preferred
		}
		if pw := g.registry.GetWorker(preferred); g.isWorkerAcceptingRequests(pw) {
			return preferred
		}
	}
	if requireHealthy {
		healthy := g.registry.GetHealthyWorkers(nil)
		candidates := make([]string, 0, len(healthy))
		for _, worker := range healthy {
			if g.extractor.HasWorker(worker.ID) {
				candidates = append(candidates, worker.ID)
			}
		}
		if len(candidates) > 0 {
			return candidates[rand.Intn(len(candidates))]
		}
	}
	return g.extractor.PickWorker("")
}

func (g *Gateway) handleStream(w http.ResponseWriter, r *http.Request) {
	if g.cfg.ExtractorIPCEnabled {
		if !g.requireIPCReady(w) {
			return
		}
		g.handleStreamViaExtractorIPC(w, r)
		return
	}
	g.proxyWithRotation(w, r, r.URL.Path)
}

func extractKeyFromDownloadLink(link string) string {
	s := strings.TrimSpace(link)
	if s == "" {
		return ""
	}
	const prefix = "/download?key="
	if strings.HasPrefix(s, prefix) {
		return strings.TrimSpace(strings.TrimPrefix(s, prefix))
	}
	if i := strings.Index(s, "key="); i >= 0 {
		return strings.TrimSpace(s[i+4:])
	}
	return s
}

func qualityLabel(raw string) string {
	q := strings.TrimSpace(strings.ToLower(raw))
	if q == "" {
		return "1080p"
	}
	if strings.HasSuffix(q, "p") {
		return q
	}
	if _, err := strconv.Atoi(q); err == nil {
		return q + "p"
	}
	return q
}

func pickStreamDownloadKey(fetchResult map[string]any, path, quality string) string {
	links, _ := fetchResult["download_links"].(map[string]any)
	if len(links) == 0 {
		return ""
	}
	if path == "/stream/mp3" || path == "/stream/mp3-chunked" || path == "/stream/m4a" {
		if mp3, _ := links["mp3"].(string); mp3 != "" {
			return extractKeyFromDownloadLink(mp3)
		}
		return ""
	}

	videoAny, _ := links["video"].(map[string]any)
	if len(videoAny) == 0 {
		return ""
	}
	target := qualityLabel(quality)
	if linkAny, ok := videoAny[target]; ok {
		if link, _ := linkAny.(string); link != "" {
			return extractKeyFromDownloadLink(link)
		}
	}
	// Fallback to commonly available qualities.
	for _, q := range []string{"1080p", "720p", "480p", "360p", "best"} {
		if linkAny, ok := videoAny[q]; ok {
			if link, _ := linkAny.(string); link != "" {
				return extractKeyFromDownloadLink(link)
			}
		}
	}
	for _, v := range videoAny {
		if link, _ := v.(string); link != "" {
			return extractKeyFromDownloadLink(link)
		}
	}
	return ""
}

func ffmpegHeaderString(headers map[string]string) string {
	if len(headers) == 0 {
		return ""
	}
	var b strings.Builder
	for k, v := range headers {
		if strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
			continue
		}
		b.WriteString(k)
		b.WriteString(": ")
		b.WriteString(v)
		b.WriteString("\r\n")
	}
	return b.String()
}

type planSourceSelector func(map[string]any) (string, map[string]string)

func selectPrimarySource(plan map[string]any) (string, map[string]string) {
	return strings.TrimSpace(anyString(plan["direct_url"])), anyMapToStringMap(plan["request_headers"])
}

func selectFFmpegAudioSource(plan map[string]any) (string, map[string]string) {
	return strings.TrimSpace(anyString(plan["ffmpeg_audio_url"])), anyMapToStringMap(plan["ffmpeg_audio_headers"])
}

func anyString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func parseContentRangeTotal(v string) int64 {
	s := strings.TrimSpace(v)
	if s == "" {
		return 0
	}
	i := strings.LastIndex(s, "/")
	if i < 0 || i+1 >= len(s) {
		return 0
	}
	total := strings.TrimSpace(s[i+1:])
	if total == "" || total == "*" {
		return 0
	}
	return parsePositiveInt64(total)
}

func sanitizeRequestHeaders(headers map[string]string) map[string]string {
	out := make(map[string]string, len(headers)+1)
	for k, v := range headers {
		kl := strings.ToLower(strings.TrimSpace(k))
		if kl == "" || strings.TrimSpace(v) == "" {
			continue
		}
		if kl == "accept-encoding" {
			continue
		}
		out[k] = v
	}
	out["Accept-Encoding"] = "identity"
	return out
}

func (g *Gateway) downloadPlanSourceToFile(
	ctx context.Context,
	dstPath string,
	plan map[string]any,
	selector planSourceSelector,
	chunkSize int64,
	canRefresh bool,
	onRefresh func() (map[string]any, *ipcError, error),
) (map[string]any, error) {
	if selector == nil {
		return plan, fmt.Errorf("missing source selector")
	}
	if chunkSize <= 0 {
		chunkSize = videoChunkSizeBytes
	}

	currentPlan := plan
	currentURL, currentHeaders := selector(currentPlan)
	currentHeaders = sanitizeRequestHeaders(currentHeaders)
	if strings.TrimSpace(currentURL) == "" {
		return currentPlan, fmt.Errorf("missing source URL in plan")
	}

	f, err := os.Create(dstPath)
	if err != nil {
		return currentPlan, err
	}
	defer f.Close()

	var read int64
	var total int64
	refreshAttempts := 0
	const maxRefreshAttempts = 3

	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, currentURL, nil)
		if err != nil {
			return currentPlan, err
		}
		for k, v := range currentHeaders {
			req.Header.Set(k, v)
		}

		useRange := read > 0 || total > 0
		if useRange {
			end := read + chunkSize - 1
			if total > 0 && end >= total {
				end = total - 1
			}
			req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", read, end))
		}

		resp, err := g.client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return currentPlan, ctx.Err()
			}
			return currentPlan, err
		}

		if resp.StatusCode == http.StatusForbidden && canRefresh && onRefresh != nil && refreshAttempts < maxRefreshAttempts {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			refreshedPlan, rpcErr, refreshErr := onRefresh()
			if refreshErr != nil {
				return currentPlan, refreshErr
			}
			if rpcErr != nil {
				return currentPlan, fmt.Errorf("%s: %s", rpcErr.Code, rpcErr.Message)
			}
			currentPlan = refreshedPlan
			currentURL, currentHeaders = selector(currentPlan)
			currentHeaders = sanitizeRequestHeaders(currentHeaders)
			if strings.TrimSpace(currentURL) == "" {
				return currentPlan, fmt.Errorf("missing refreshed source URL in plan")
			}
			refreshAttempts++
			continue
		}

		if resp.StatusCode == http.StatusRequestedRangeNotSatisfiable {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if total > 0 && read >= total {
				return currentPlan, nil
			}
			return currentPlan, fmt.Errorf("range not satisfiable at %d/%d", read, total)
		}

		if resp.StatusCode != http.StatusPartialContent && resp.StatusCode != http.StatusOK {
			bodySnippet, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
			_ = resp.Body.Close()
			return currentPlan, fmt.Errorf("upstream status %d: %s", resp.StatusCode, strings.TrimSpace(string(bodySnippet)))
		}

		if resp.StatusCode == http.StatusPartialContent {
			if total <= 0 {
				if t := parseContentRangeTotal(resp.Header.Get("Content-Range")); t > 0 {
					total = t
				} else if cl := parsePositiveInt64(resp.Header.Get("Content-Length")); cl > 0 {
					total = read + cl
				}
			}
		} else if read > 0 {
			// Upstream ignored a resume range. Restart from zero to avoid duplicated bytes.
			if _, err := f.Seek(0, io.SeekStart); err != nil {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				return currentPlan, err
			}
			if err := f.Truncate(0); err != nil {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				return currentPlan, err
			}
			read = 0
			total = parsePositiveInt64(resp.Header.Get("Content-Length"))
		}

		n, copyErr := io.Copy(f, resp.Body)
		_ = resp.Body.Close()
		if copyErr != nil {
			if ctx.Err() != nil {
				return currentPlan, ctx.Err()
			}
			return currentPlan, copyErr
		}
		read += n

		if resp.StatusCode == http.StatusOK {
			return currentPlan, nil
		}
		if total > 0 && read >= total {
			return currentPlan, nil
		}
		if n <= 0 {
			return currentPlan, nil
		}
	}
}

func (g *Gateway) downloadPlanSourceToWriter(
	ctx context.Context,
	dst io.WriteCloser,
	workerID string,
	plan map[string]any,
	selector planSourceSelector,
	chunkSize int64,
	canRefresh bool,
	onRefresh func() (map[string]any, *ipcError, error),
) error {
	if dst == nil {
		return fmt.Errorf("missing destination writer")
	}
	if selector == nil {
		return fmt.Errorf("missing source selector")
	}
	if chunkSize <= 0 {
		chunkSize = videoChunkSizeBytes
	}

	currentPlan := plan
	currentURL, currentHeaders := selector(currentPlan)
	currentHeaders = sanitizeRequestHeaders(currentHeaders)
	if strings.TrimSpace(currentURL) == "" {
		return fmt.Errorf("missing source URL in plan")
	}
	mediaClient := g.mediaHTTPClient(workerID)

	var read int64
	var total int64
	refreshAttempts := 0
	const maxRefreshAttempts = 3

	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, currentURL, nil)
		if err != nil {
			return err
		}
		for k, v := range currentHeaders {
			req.Header.Set(k, v)
		}

		useRange := read > 0 || total > 0
		if useRange {
			end := read + chunkSize - 1
			if total > 0 && end >= total {
				end = total - 1
			}
			req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", read, end))
		}

		resp, err := mediaClient.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}

		if resp.StatusCode == http.StatusForbidden && canRefresh && onRefresh != nil && refreshAttempts < maxRefreshAttempts {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			refreshedPlan, rpcErr, refreshErr := onRefresh()
			if refreshErr != nil {
				return refreshErr
			}
			if rpcErr != nil {
				return fmt.Errorf("%s: %s", rpcErr.Code, rpcErr.Message)
			}
			currentPlan = refreshedPlan
			currentURL, currentHeaders = selector(currentPlan)
			currentHeaders = sanitizeRequestHeaders(currentHeaders)
			if strings.TrimSpace(currentURL) == "" {
				return fmt.Errorf("missing refreshed source URL in plan")
			}
			refreshAttempts++
			continue
		}

		if resp.StatusCode == http.StatusRequestedRangeNotSatisfiable {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if total > 0 && read >= total {
				return nil
			}
			return fmt.Errorf("range not satisfiable at %d/%d", read, total)
		}

		if resp.StatusCode != http.StatusPartialContent && resp.StatusCode != http.StatusOK {
			bodySnippet, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
			_ = resp.Body.Close()
			return fmt.Errorf("upstream status %d: %s", resp.StatusCode, strings.TrimSpace(string(bodySnippet)))
		}

		if resp.StatusCode == http.StatusPartialContent {
			if total <= 0 {
				if t := parseContentRangeTotal(resp.Header.Get("Content-Range")); t > 0 {
					total = t
				} else if cl := parsePositiveInt64(resp.Header.Get("Content-Length")); cl > 0 {
					total = read + cl
				}
			}
		} else if read > 0 {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			return fmt.Errorf("upstream ignored resume range at offset %d", read)
		}

		n, copyErr := io.Copy(dst, resp.Body)
		_ = resp.Body.Close()
		if copyErr != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return copyErr
		}
		read += n

		if resp.StatusCode == http.StatusOK {
			return nil
		}
		if total > 0 && read >= total {
			return nil
		}
		if n <= 0 {
			return nil
		}
	}
}

func (g *Gateway) streamWithFFmpegFromPlan(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan map[string]any,
	onRefresh func() (map[string]any, *ipcError, error),
) bool {
	directURL, _ := plan["direct_url"].(string)
	if strings.TrimSpace(directURL) == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return false
	}
	requestHeaders := anyMapToStringMap(plan["request_headers"])
	audioURL, _ := plan["ffmpeg_audio_url"].(string)
	audioHeaders := anyMapToStringMap(plan["ffmpeg_audio_headers"])
	responseHeaders := anyMapToStringMap(plan["response_headers"])
	mediaType, _ := plan["media_type"].(string)
	audioOnly, _ := plan["ffmpeg_audio_only"].(bool)
	mergeAV, _ := plan["ffmpeg_merge"].(bool)
	canRefresh, _ := plan["can_refresh"].(bool)

	ffmpegArgs := []string{"-hide_banner", "-loglevel", "error"}
	var stdinSource io.Reader
	var extraFiles []*os.File
	feedWorkers := 0
	feedErrCh := make(chan error, 2)
	startFeeds := func() {}
	if audioOnly {
		pipeR, pipeW := io.Pipe()
		stdinSource = pipeR
		ffmpegArgs = append(ffmpegArgs, "-i", "pipe:0")
		feedWorkers = 1
		startFeeds = func() {
			go func() {
				err := g.downloadPlanSourceToWriter(
					r.Context(),
					pipeW,
					workerID,
					plan,
					selectPrimarySource,
					audioChunkSizeBytes,
					canRefresh,
					onRefresh,
				)
				_ = pipeW.CloseWithError(err)
				feedErrCh <- err
			}()
		}
	} else if mergeAV {
		videoR, videoW := io.Pipe()
		audioR, audioW, err := os.Pipe()
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "Failed to create ffmpeg audio pipe", "detail": err.Error()})
			return false
		}
		stdinSource = videoR
		extraFiles = []*os.File{audioR}
		ffmpegArgs = append(ffmpegArgs, "-i", "pipe:0", "-i", "pipe:3")
		feedWorkers = 2
		startFeeds = func() {
			go func() {
				err := g.downloadPlanSourceToWriter(
					r.Context(),
					videoW,
					workerID,
					plan,
					selectPrimarySource,
					videoChunkSizeBytes,
					canRefresh,
					onRefresh,
				)
				_ = videoW.CloseWithError(err)
				feedErrCh <- err
			}()
			go func() {
				err := g.downloadPlanSourceToWriter(
					r.Context(),
					audioW,
					workerID,
					plan,
					selectFFmpegAudioSource,
					audioChunkSizeBytes,
					canRefresh,
					onRefresh,
				)
				_ = audioW.Close()
				feedErrCh <- err
			}()
		}
		defer audioR.Close()
	} else {
		if hdr := ffmpegHeaderString(requestHeaders); hdr != "" {
			ffmpegArgs = append(ffmpegArgs, "-headers", hdr)
		}
		ffmpegArgs = append(ffmpegArgs, "-i", directURL)
		if mergeAV && strings.TrimSpace(audioURL) != "" {
			if hdr := ffmpegHeaderString(audioHeaders); hdr != "" {
				ffmpegArgs = append(ffmpegArgs, "-headers", hdr)
			}
			ffmpegArgs = append(ffmpegArgs, "-i", audioURL)
		}
	}
	if audioOnly {
		ffmpegArgs = append(ffmpegArgs, "-vn", "-acodec", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1")
		if mediaType == "" {
			mediaType = "audio/mpeg"
		}
	} else if mergeAV && strings.TrimSpace(audioURL) != "" {
		ffmpegArgs = append(
			ffmpegArgs,
			"-map", "0:v:0",
			"-map", "1:a:0",
			"-c:v", "copy",
			"-c:a", "aac",
			"-movflags", "frag_keyframe+empty_moov+faststart",
			"-f", "mp4",
			"pipe:1",
		)
		if mediaType == "" {
			mediaType = "video/mp4"
		}
	} else {
		ffmpegArgs = append(ffmpegArgs, "-c", "copy", "-movflags", "frag_keyframe+empty_moov+faststart", "-f", "mp4", "pipe:1")
		if mediaType == "" {
			mediaType = "video/mp4"
		}
	}

	cmd := exec.CommandContext(r.Context(), "ffmpeg", ffmpegArgs...)
	if stdinSource != nil {
		cmd.Stdin = stdinSource
	}
	if len(extraFiles) > 0 {
		cmd.ExtraFiles = extraFiles
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to create ffmpeg pipe", "detail": err.Error()})
		return false
	}
	var ffmpegErr bytes.Buffer
	cmd.Stderr = &ffmpegErr

	if err := cmd.Start(); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to start ffmpeg", "detail": err.Error()})
		return false
	}
	startFeeds()

	for k, v := range responseHeaders {
		w.Header().Set(k, v)
	}
	if w.Header().Get("Content-Type") == "" && mediaType != "" {
		w.Header().Set("Content-Type", mediaType)
	}
	w.WriteHeader(http.StatusOK)
	_, copyErr := io.Copy(w, stdout)
	waitErr := cmd.Wait()
	for i := 0; i < feedWorkers; i++ {
		if feedErr := <-feedErrCh; feedErr != nil && r.Context().Err() == nil && !isClientAbortError(feedErr) {
			log.Printf("ffmpeg input feed error: %v", feedErr)
		}
	}

	if copyErr != nil && !isClientAbortError(copyErr) {
		log.Printf("ffmpeg stream copy error: %v", copyErr)
	}
	if waitErr != nil && r.Context().Err() == nil && !isClientAbortError(waitErr) {
		log.Printf("ffmpeg stream error: %v, out=%s", waitErr, strings.TrimSpace(ffmpegErr.String()))
	}
	return true
}

func (g *Gateway) streamDirectPlanWithRefresh(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan map[string]any,
	onRefresh func() (map[string]any, *ipcError, error),
) bool {
	directURL, _ := plan["direct_url"].(string)
	if directURL == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return false
	}
	requestHeaders := anyMapToStringMap(plan["request_headers"])
	responseHeaders := anyMapToStringMap(plan["response_headers"])
	mediaType, _ := plan["media_type"].(string)
	canRefresh, _ := plan["can_refresh"].(bool)

	maxAttempts := 1
	if canRefresh && onRefresh != nil {
		maxAttempts = 2
	}

	currentURL := directURL
	currentPlan := plan
	currentReqHeaders := requestHeaders
	currentRespHeaders := responseHeaders
	currentMediaType := mediaType

	for attempt := 0; attempt < maxAttempts; attempt++ {
		mediaClient := g.mediaHTTPClientForPlan(workerID, currentPlan)
		req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, currentURL, nil)
		if err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to create upstream request", "detail": err.Error()})
			return false
		}
		for k, v := range currentReqHeaders {
			req.Header.Set(k, v)
		}
		// Preserve range semantics for direct media.
		if req.Header.Get("Range") == "" {
			if v := r.Header.Get("Range"); v != "" {
				req.Header.Set("Range", v)
			}
		}
		if req.Header.Get("If-Range") == "" {
			if v := r.Header.Get("If-Range"); v != "" {
				req.Header.Set("If-Range", v)
			}
		}

		resp, err := mediaClient.Do(req)
		if err != nil {
			if r.Context().Err() != nil || isClientAbortError(err) {
				return true
			}
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Upstream request failed", "detail": err.Error()})
			return false
		}

		if resp.StatusCode == http.StatusForbidden && attempt == 0 && canRefresh && onRefresh != nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()

			refreshedPlan, rpcErr, err := onRefresh()
			if err != nil || rpcErr != nil {
				break
			}
			if newURL, _ := refreshedPlan["direct_url"].(string); newURL != "" {
				currentURL = newURL
			}
			currentPlan = refreshedPlan
			currentReqHeaders = anyMapToStringMap(refreshedPlan["request_headers"])
			currentRespHeaders = anyMapToStringMap(refreshedPlan["response_headers"])
			if mt, _ := refreshedPlan["media_type"].(string); mt != "" {
				currentMediaType = mt
			}
			continue
		}

		if resp.StatusCode >= 400 {
			copyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true})
			w.WriteHeader(resp.StatusCode)
			_, _ = io.Copy(w, resp.Body)
			_ = resp.Body.Close()
			return false
		}

		for k, v := range currentRespHeaders {
			w.Header().Set(k, v)
		}
		if w.Header().Get("Content-Type") == "" {
			if currentMediaType != "" {
				w.Header().Set("Content-Type", currentMediaType)
			} else if ct := resp.Header.Get("Content-Type"); ct != "" {
				w.Header().Set("Content-Type", ct)
			}
		}
		if cr := resp.Header.Get("Content-Range"); cr != "" {
			w.Header().Set("Content-Range", cr)
		}
		if ar := resp.Header.Get("Accept-Ranges"); ar != "" {
			w.Header().Set("Accept-Ranges", ar)
		}
		if cl := resp.Header.Get("Content-Length"); cl != "" && w.Header().Get("Content-Length") == "" {
			w.Header().Set("Content-Length", cl)
		}

		w.WriteHeader(resp.StatusCode)
		_, copyErr := io.Copy(w, resp.Body)
		_ = resp.Body.Close()
		if copyErr != nil && !isClientAbortError(copyErr) {
			log.Printf("download stream copy error: %v", copyErr)
		}
		return true
	}

	w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "Download failed",
	})
	return false
}

func parsePositiveInt64(v string) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
	if err != nil || n <= 0 {
		return 0
	}
	return n
}

func (g *Gateway) streamChunkedRangeWithRefresh(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan map[string]any,
	onRefresh func() (map[string]any, *ipcError, error),
	chunkSize int64,
) bool {
	directURL, _ := plan["direct_url"].(string)
	if strings.TrimSpace(directURL) == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return false
	}
	requestHeaders := anyMapToStringMap(plan["request_headers"])
	responseHeaders := anyMapToStringMap(plan["response_headers"])
	mediaType, _ := plan["media_type"].(string)
	canRefresh, _ := plan["can_refresh"].(bool)
	if chunkSize <= 0 {
		chunkSize = videoChunkSizeBytes
	}

	totalSize := parsePositiveInt64(responseHeaders["Content-Length"])
	if totalSize <= 0 {
		// Without exact size, keep compatibility by falling back to direct stream.
		return g.streamDirectPlanWithRefresh(w, r, workerID, plan, onRefresh)
	}

	for k, v := range responseHeaders {
		kl := strings.ToLower(k)
		if kl == "content-length" || kl == "content-range" {
			continue
		}
		w.Header().Set(k, v)
	}
	if w.Header().Get("Content-Type") == "" && mediaType != "" {
		w.Header().Set("Content-Type", mediaType)
	}
	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Content-Length", strconv.FormatInt(totalSize, 10))
	w.WriteHeader(http.StatusOK)

	currentURL := directURL
	currentReqHeaders := requestHeaders
	read := int64(0)
	mediaClient := g.mediaHTTPClient(workerID)

	for read < totalSize {
		end := read + chunkSize - 1
		if end >= totalSize {
			end = totalSize - 1
		}

		req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, currentURL, nil)
		if err != nil {
			return false
		}
		for k, v := range currentReqHeaders {
			req.Header.Set(k, v)
		}
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", read, end))

		resp, err := mediaClient.Do(req)
		if err != nil {
			if r.Context().Err() != nil || isClientAbortError(err) {
				return true
			}
			return false
		}

		if resp.StatusCode == http.StatusForbidden && canRefresh && onRefresh != nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			refreshedPlan, rpcErr, refreshErr := onRefresh()
			if refreshErr != nil || rpcErr != nil {
				return false
			}
			if newURL, _ := refreshedPlan["direct_url"].(string); strings.TrimSpace(newURL) != "" {
				currentURL = newURL
			}
			currentReqHeaders = anyMapToStringMap(refreshedPlan["request_headers"])
			continue
		}

		if resp.StatusCode != http.StatusPartialContent && !(resp.StatusCode == http.StatusOK && read == 0) {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			return false
		}

		n, copyErr := io.Copy(w, resp.Body)
		_ = resp.Body.Close()
		if copyErr != nil {
			if isClientAbortError(copyErr) || r.Context().Err() != nil {
				return true
			}
			return false
		}
		read += n

		// Some servers ignore range and stream whole body on first request.
		if resp.StatusCode == http.StatusOK {
			return true
		}
		if n <= 0 {
			break
		}
	}
	return true
}

func (g *Gateway) handleDownloadViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	g.handleDownloadViaExtractorIPCWithDefault(w, r, true)
}

func (g *Gateway) handleDownloadViaExtractorIPCWithDefault(w http.ResponseWriter, r *http.Request, defaultDownload bool) {
	key := strings.TrimSpace(r.URL.Query().Get("key"))
	if key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing key query parameter"})
		return
	}
	download := parseBoolQuery(r.URL.Query().Get("download"), defaultDownload)
	preferred := extractWorkerID(key, g.cfg.WorkerCount)
	workerID := g.selectExtractorWorker(preferred, false)
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}

	plan, rpcErr, err := g.extractor.DownloadPrepare(workerID, key, download)
	if err != nil {
		log.Printf("[extractor:%s] download prepare ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}

	if needsFFmpeg, _ := plan["needs_ffmpeg"].(bool); needsFFmpeg {
		_ = g.streamWithFFmpegFromPlan(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
			return g.extractor.DownloadRefresh(workerID, key, download)
		})
		return
	}
	sessionType := anyString(plan["session_type"])
	deliveryMode := anyString(plan["delivery_mode"])
	if deliveryMode == "single_progressive" && sessionType == "video" {
		_ = g.streamChunkedRangeWithRefresh(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
			return g.extractor.DownloadRefresh(workerID, key, download)
		}, int64(videoChunkSizeBytes))
		return
	}
	_ = g.streamDirectPlanWithRefresh(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
		return g.extractor.DownloadRefresh(workerID, key, download)
	})
}

func (g *Gateway) handleStreamViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("url"))
	if url == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing url query parameter"})
		return
	}
	proxy := strings.TrimSpace(r.URL.Query().Get("proxy"))
	impersonate := strings.TrimSpace(r.URL.Query().Get("impersonate"))
	download := parseBoolQuery(r.URL.Query().Get("download"), false)

	workerID := g.selectExtractorWorker("", true)
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}

	// m4a has special parity path: resolve bestaudio m4a directly (no mp3 transcode).
	if r.URL.Path == "/stream/m4a" {
		resolved, rpcErr, err := g.extractor.ResolveFormats(workerID, url, "bestaudio[ext=m4a]/bestaudio", proxy, impersonate)
		if err != nil {
			log.Printf("[extractor:%s] stream m4a resolve ipc error: %v", workerID, err)
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
			return
		}
		if rpcErr != nil {
			status := rpcErr.Status
			if status < 400 || status > 599 {
				status = http.StatusBadRequest
			}
			writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
			return
		}
		formatsAny, _ := resolved["formats"].([]any)
		if len(formatsAny) == 0 {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "No resolved audio formats"})
			return
		}
		fmt0, _ := formatsAny[0].(map[string]any)
		directURL, _ := fmt0["url"].(string)
		if strings.TrimSpace(directURL) == "" {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid resolved audio URL"})
			return
		}
		reqHeaders := anyMapToStringMap(fmt0["http_headers"])
		plan := map[string]any{
			"direct_url":       directURL,
			"request_headers":  reqHeaders,
			"response_headers": map[string]any{"X-Accel-Buffering": "no"},
			"media_type":       "audio/mp4",
			"can_refresh":      false,
		}
		if download {
			plan["response_headers"] = map[string]any{
				"Content-Disposition": "attachment; filename=\"audio.m4a\"",
				"X-Accel-Buffering":   "no",
			}
		}
		_ = g.streamDirectPlanWithRefresh(w, r, workerID, plan, nil)
		return
	}

	fetchResult, rpcErr, err := g.extractor.Fetch(workerID, url, proxy, impersonate)
	if err != nil {
		log.Printf("[extractor:%s] stream fetch ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}

	key := pickStreamDownloadKey(fetchResult, r.URL.Path, r.URL.Query().Get("quality"))
	if key == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Unable to resolve stream key from fetch result"})
		return
	}

	plan, rpcErr, err := g.extractor.DownloadPrepare(workerID, key, download)
	if err != nil {
		log.Printf("[extractor:%s] stream download prepare ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}

	if needsFFmpeg, _ := plan["needs_ffmpeg"].(bool); needsFFmpeg {
		_ = g.streamWithFFmpegFromPlan(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
			return g.extractor.DownloadRefresh(workerID, key, download)
		})
		return
	}

	deliveryMode := anyString(plan["delivery_mode"])
	if deliveryMode == "single_progressive" {
		if r.URL.Path == "/stream/video-chunked" || r.URL.Path == "/stream/mp3-chunked" {
			chunkSize := int64(videoChunkSizeBytes)
			if r.URL.Path == "/stream/mp3-chunked" {
				chunkSize = audioChunkSizeBytes
			}
			_ = g.streamChunkedRangeWithRefresh(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
				return g.extractor.DownloadRefresh(workerID, key, download)
			}, chunkSize)
			return
		}
		_ = g.streamDirectPlanWithRefresh(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
			return g.extractor.DownloadRefresh(workerID, key, download)
		})
		return
	}

	if r.URL.Path == "/stream/video-chunked" || r.URL.Path == "/stream/mp3-chunked" {
		chunkSize := int64(videoChunkSizeBytes)
		if r.URL.Path == "/stream/mp3-chunked" {
			chunkSize = audioChunkSizeBytes
		}
		_ = g.streamChunkedRangeWithRefresh(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
			return g.extractor.DownloadRefresh(workerID, key, download)
		}, chunkSize)
		return
	}
	_ = g.streamDirectPlanWithRefresh(w, r, workerID, plan, func() (map[string]any, *ipcError, error) {
		return g.extractor.DownloadRefresh(workerID, key, download)
	})
}

func (g *Gateway) handleTikTok(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Method not allowed"})
		return
	}
	if g.cfg.ExtractorIPCEnabled {
		if !g.requireIPCReady(w) {
			return
		}
		g.handleTikTokViaExtractorIPC(w, r)
		return
	}
	g.proxyWithRetry(w, r, "/tiktok", http.MethodPost, "", false)
}

type tiktokIPCRequest struct {
	URL         string `json:"url"`
	Proxy       string `json:"proxy"`
	Impersonate string `json:"impersonate"`
}

func (g *Gateway) handleTikTokViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	var payload tiktokIPCRequest
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Invalid JSON body"})
		return
	}
	payload.URL = strings.TrimSpace(payload.URL)
	payload.Proxy = strings.TrimSpace(payload.Proxy)
	payload.Impersonate = strings.TrimSpace(payload.Impersonate)
	if payload.URL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "URL is required"})
		return
	}

	workerID := g.selectExtractorWorker("", true)
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}

	result, rpcErr, err := g.extractor.TikTok(workerID, payload.URL, payload.Proxy, payload.Impersonate)
	if err != nil {
		log.Printf("[extractor:%s] tiktok ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{
			"error":  rpcErr.Code,
			"detail": rpcErr.Message,
		})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (g *Gateway) handleTikTokDownload(w http.ResponseWriter, r *http.Request) {
	if g.cfg.ExtractorIPCEnabled {
		if !g.requireIPCReady(w) {
			return
		}
		g.handleTikTokDownloadViaExtractorIPC(w, r)
		return
	}
	// Download keys are worker-affine (`wX::...`), so keep routing strict to owner.
	g.proxyWithRetry(w, r, "/tiktok/download", http.MethodGet, extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount), true)
}

func parseBoolQuery(raw string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(raw))
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func anyMapToStringMap(v any) map[string]string {
	out := map[string]string{}
	obj, ok := v.(map[string]any)
	if !ok {
		return out
	}
	for k, val := range obj {
		if s, ok := val.(string); ok {
			out[k] = s
		}
	}
	return out
}

func anyToStringSlice(v any) []string {
	if direct, ok := v.([]string); ok {
		out := make([]string, 0, len(direct))
		for _, s := range direct {
			if strings.TrimSpace(s) != "" {
				out = append(out, s)
			}
		}
		return out
	}
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
			out = append(out, s)
		}
	}
	return out
}

func anyToInt(v any, fallback int) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(t))
		if err == nil {
			return n
		}
	}
	return fallback
}

func shouldRefreshTikTokForbidden(body []byte) bool {
	text := strings.ToLower(string(body))
	if strings.TrimSpace(text) == "" {
		return true
	}
	permanentPatterns := []string{
		"geo_restricted",
		"geo restricted",
		"region",
		"country",
		"blocked",
		"permission",
		"do not have permission",
		"login",
		"log in",
		"captcha",
		"verify you are human",
		"access denied",
	}
	for _, p := range permanentPatterns {
		if strings.Contains(text, p) {
			return false
		}
	}
	return true
}

func (g *Gateway) handleTikTokDownloadViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimSpace(r.URL.Query().Get("key"))
	if key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing key query parameter"})
		return
	}
	download := parseBoolQuery(r.URL.Query().Get("download"), true)
	preferred := extractWorkerID(key, g.cfg.WorkerCount)
	workerID := g.selectExtractorWorker(preferred, false)
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}

	plan, rpcErr, err := g.extractor.TikTokDownloadPrepare(workerID, key, download)
	if err != nil {
		log.Printf("[extractor:%s] tiktok download prepare ipc error: %v", workerID, err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{
			"error":  rpcErr.Code,
			"detail": rpcErr.Message,
		})
		return
	}

	if fallback, _ := plan["fallback_proxy"].(bool); fallback {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error":  "fallback_proxy_not_allowed",
			"detail": "EXTRACTOR_IPC_ENABLED=true requires full IPC mode",
		})
		return
	}
	if contentType, _ := plan["content_type"].(string); contentType == "slideshow" {
		g.streamTikTokSlideshowFromPlan(w, r, workerID, key, download, preferred, plan)
		return
	}

	directURL, _ := plan["direct_url"].(string)
	if directURL == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return
	}
	requestHeaders := anyMapToStringMap(plan["request_headers"])
	responseHeaders := anyMapToStringMap(plan["response_headers"])
	mediaType, _ := plan["media_type"].(string)
	canRefresh, _ := plan["can_refresh"].(bool)

	maxAttempts := 1
	if canRefresh {
		maxAttempts = 2
	}

	currentURL := directURL
	currentPlan := plan
	currentReqHeaders := requestHeaders
	currentRespHeaders := responseHeaders
	currentMediaType := mediaType

	for attempt := 0; attempt < maxAttempts; attempt++ {
		mediaClient := g.mediaHTTPClientForPlan(workerID, currentPlan)
		req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, currentURL, nil)
		if err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to create upstream request", "detail": err.Error()})
			return
		}
		for k, v := range currentReqHeaders {
			req.Header.Set(k, v)
		}

		resp, err := mediaClient.Do(req)
		if err != nil {
			if r.Context().Err() != nil || isClientAbortError(err) {
				return
			}
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Upstream request failed", "detail": err.Error()})
			return
		}

		if resp.StatusCode == http.StatusForbidden && attempt == 0 && canRefresh {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
			_ = resp.Body.Close()
			if !shouldRefreshTikTokForbidden(body) {
				copyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true})
				w.WriteHeader(resp.StatusCode)
				if len(body) > 0 {
					_, _ = w.Write(body)
				}
				return
			}

			refreshedPlan, rpcErr, err := g.extractor.TikTokDownloadRefresh(workerID, key, download)
			if err != nil {
				log.Printf("[extractor:%s] tiktok download refresh ipc error: %v", workerID, err)
				break
			}
			if rpcErr != nil {
				break
			}
			if fallback, _ := refreshedPlan["fallback_proxy"].(bool); fallback {
				writeJSON(w, http.StatusBadGateway, map[string]string{
					"error":  "fallback_proxy_not_allowed",
					"detail": "EXTRACTOR_IPC_ENABLED=true requires full IPC mode",
				})
				return
			}
			if newURL, _ := refreshedPlan["direct_url"].(string); newURL != "" {
				currentURL = newURL
			}
			currentPlan = refreshedPlan
			currentReqHeaders = anyMapToStringMap(refreshedPlan["request_headers"])
			currentRespHeaders = anyMapToStringMap(refreshedPlan["response_headers"])
			if mt, _ := refreshedPlan["media_type"].(string); mt != "" {
				currentMediaType = mt
			}
			continue
		}

		if resp.StatusCode >= 400 {
			copyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true})
			w.WriteHeader(resp.StatusCode)
			_, _ = io.Copy(w, resp.Body)
			_ = resp.Body.Close()
			return
		}

		for k, v := range currentRespHeaders {
			w.Header().Set(k, v)
		}
		if w.Header().Get("Content-Type") == "" {
			if currentMediaType != "" {
				w.Header().Set("Content-Type", currentMediaType)
			} else if ct := resp.Header.Get("Content-Type"); ct != "" {
				w.Header().Set("Content-Type", ct)
			}
		}
		// Preserve range semantics when upstream supports it.
		if cr := resp.Header.Get("Content-Range"); cr != "" {
			w.Header().Set("Content-Range", cr)
		}
		if ar := resp.Header.Get("Accept-Ranges"); ar != "" {
			w.Header().Set("Accept-Ranges", ar)
		}
		if cl := resp.Header.Get("Content-Length"); cl != "" && w.Header().Get("Content-Length") == "" {
			w.Header().Set("Content-Length", cl)
		}

		w.WriteHeader(resp.StatusCode)
		_, copyErr := io.Copy(w, resp.Body)
		_ = resp.Body.Close()
		if copyErr != nil && !isClientAbortError(copyErr) {
			log.Printf("[extractor:%s] tiktok download stream copy error: %v", workerID, copyErr)
		}
		return
	}

	w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "TikTok download failed",
	})
}

func (g *Gateway) downloadToFile(ctx context.Context, srcURL string, headers map[string]string, dstPath string) error {
	return g.downloadToFileWithClient(ctx, g.client, srcURL, headers, dstPath)
}

func (g *Gateway) downloadToFileWithClient(ctx context.Context, client *http.Client, srcURL string, headers map[string]string, dstPath string) error {
	if client == nil {
		client = g.client
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, srcURL, nil)
	if err != nil {
		return err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("upstream %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	f, err := os.Create(dstPath)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, resp.Body)
	return err
}

func (g *Gateway) streamTikTokSlideshowFromPlan(
	w http.ResponseWriter,
	r *http.Request,
	workerID, key string,
	download bool,
	preferred string,
	plan map[string]any,
) {
	currentPlan := plan
	for attempt := 0; attempt < 2; attempt++ {
		mediaClient := g.mediaHTTPClientForPlan(workerID, currentPlan)
		outputPath, mediaType, responseHeaders, tempDir, statusCode, err := g.renderTikTokSlideshowFromPlan(r.Context(), mediaClient, currentPlan)
		if err == nil {
			defer os.RemoveAll(tempDir)
			for k, v := range responseHeaders {
				w.Header().Set(k, v)
			}
			w.Header().Set("Content-Type", mediaType)

			f, openErr := os.Open(outputPath)
			if openErr != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "Failed to open slideshow output", "detail": openErr.Error()})
				return
			}
			defer f.Close()
			if fi, statErr := f.Stat(); statErr == nil && fi.Size() > 0 {
				w.Header().Set("Content-Length", strconv.FormatInt(fi.Size(), 10))
			}
			w.WriteHeader(http.StatusOK)
			_, copyErr := io.Copy(w, f)
			if copyErr != nil && !isClientAbortError(copyErr) {
				log.Printf("slideshow stream copy error: %v", copyErr)
			}
			return
		}

		if tempDir != "" {
			_ = os.RemoveAll(tempDir)
		}

		// Slideshow URLs can expire quickly. Retry once with a fresh extraction.
		if attempt == 0 && strings.TrimSpace(workerID) != "" && strings.TrimSpace(key) != "" && g.extractor != nil {
			refreshedPlan, rpcErr, refreshErr := g.extractor.TikTokDownloadRefresh(workerID, key, download)
			if refreshErr == nil && rpcErr == nil {
				if fallback, _ := refreshedPlan["fallback_proxy"].(bool); fallback {
					writeJSON(w, http.StatusBadGateway, map[string]string{
						"error":  "fallback_proxy_not_allowed",
						"detail": "EXTRACTOR_IPC_ENABLED=true requires full IPC mode",
					})
					return
				}
				currentPlan = refreshedPlan
				continue
			}
		}

		if statusCode < 400 || statusCode > 599 {
			statusCode = http.StatusBadGateway
		}
		writeJSON(w, statusCode, map[string]string{
			"error":  "Failed to render slideshow",
			"detail": err.Error(),
		})
		return
	}
	writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to render slideshow", "detail": "Unknown slideshow rendering failure"})
}

func (g *Gateway) renderTikTokSlideshowFromPlan(ctx context.Context, mediaClient *http.Client, plan map[string]any) (string, string, map[string]string, string, int, error) {
	photoURLs := anyToStringSlice(plan["photo_urls"])
	audioURL, _ := plan["audio_url"].(string)
	if len(photoURLs) == 0 {
		return "", "", nil, "", http.StatusBadRequest, fmt.Errorf("no photos available for slideshow")
	}
	durationPerImage := anyToInt(plan["duration_per_image"], 4)
	if durationPerImage < 1 {
		durationPerImage = 4
	}
	responseHeaders := anyMapToStringMap(plan["response_headers"])
	mediaType, _ := plan["media_type"].(string)
	if mediaType == "" {
		mediaType = "video/mp4"
	}

	tempDir, err := os.MkdirTemp("", "tiktok_slideshow_")
	if err != nil {
		return "", "", nil, "", http.StatusInternalServerError, fmt.Errorf("failed to create temp dir: %w", err)
	}

	imagePaths := make([]string, 0, len(photoURLs))
	for i, u := range photoURLs {
		dst := filepath.Join(tempDir, fmt.Sprintf("image_%d.jpg", i))
		if err := g.downloadToFileWithClient(ctx, mediaClient, u, map[string]string{}, dst); err != nil {
			return "", "", nil, tempDir, http.StatusBadGateway, fmt.Errorf("failed to download slideshow image: %w", err)
		}
		imagePaths = append(imagePaths, dst)
	}

	audioPath := ""
	if strings.TrimSpace(audioURL) != "" {
		audioPath = filepath.Join(tempDir, "audio.mp3")
		if err := g.downloadToFileWithClient(ctx, mediaClient, audioURL, map[string]string{}, audioPath); err != nil {
			return "", "", nil, tempDir, http.StatusBadGateway, fmt.Errorf("failed to download slideshow audio: %w", err)
		}
	}

	outputPath := filepath.Join(tempDir, "slideshow.mp4")
	ffmpegArgs := []string{"-y", "-hide_banner", "-loglevel", "error"}
	for _, img := range imagePaths {
		ffmpegArgs = append(ffmpegArgs, "-loop", "1", "-t", strconv.Itoa(durationPerImage), "-i", img)
	}
	audioInputIndex := -1
	if audioPath != "" {
		audioInputIndex = len(imagePaths)
		ffmpegArgs = append(ffmpegArgs, "-stream_loop", "-1", "-i", audioPath)
	}

	filterParts := make([]string, 0, len(imagePaths)+2)
	concatInputs := make([]string, 0, len(imagePaths))
	for i := range imagePaths {
		filterParts = append(filterParts,
			fmt.Sprintf(
				"[%d:v]scale=w=720:h=1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=24,trim=duration=%d,setpts=PTS-STARTPTS[v%d]",
				i,
				durationPerImage,
				i,
			),
		)
		concatInputs = append(concatInputs, fmt.Sprintf("[v%d]", i))
	}
	filterParts = append(filterParts, fmt.Sprintf("%sconcat=n=%d:v=1:a=0[vout]", strings.Join(concatInputs, ""), len(imagePaths)))

	if audioPath != "" {
		videoDuration := len(imagePaths) * durationPerImage
		filterParts = append(filterParts, fmt.Sprintf("[%d:a]atrim=0:%d,asetpts=PTS-STARTPTS[aout]", audioInputIndex, videoDuration))
		ffmpegArgs = append(ffmpegArgs,
			"-filter_complex", strings.Join(filterParts, ";"),
			"-map", "[vout]",
			"-map", "[aout]",
			"-pix_fmt", "yuv420p",
			"-fps_mode", "cfr",
			"-c:v", "libx264",
			"-preset", "medium",
			"-crf", "23",
			"-b:v", "320k",
			"-maxrate", "360k",
			"-bufsize", "720k",
			"-threads", "1",
			"-c:a", "aac",
			"-b:a", "128k",
			outputPath,
		)
	} else {
		ffmpegArgs = append(ffmpegArgs,
			"-filter_complex", strings.Join(filterParts, ";"),
			"-map", "[vout]",
			"-pix_fmt", "yuv420p",
			"-fps_mode", "cfr",
			"-c:v", "libx264",
			"-preset", "medium",
			"-crf", "23",
			"-b:v", "320k",
			"-maxrate", "360k",
			"-bufsize", "720k",
			"-threads", "1",
			outputPath,
		)
	}

	cmd := exec.CommandContext(ctx, "ffmpeg", ffmpegArgs...)
	if out, err := cmd.CombinedOutput(); err != nil {
		detail := strings.TrimSpace(string(out))
		log.Printf("slideshow ffmpeg error: %v, out=%s", err, detail)
		return "", "", nil, tempDir, http.StatusBadGateway, fmt.Errorf("ffmpeg failed: %s", detail)
	}
	return outputPath, mediaType, responseHeaders, tempDir, http.StatusOK, nil
}

func (g *Gateway) handleHealth(w http.ResponseWriter, _ *http.Request) {
	healthy := g.registry.GetHealthyWorkers(nil)
	now := time.Now()
	workers := g.registry.WorkersSnapshot()
	rows := make([]map[string]any, 0, len(workers))
	for _, worker := range workers {
		rows = append(rows, map[string]any{
			"id":                        worker.ID,
			"healthy":                   worker.Healthy,
			"restarting":                worker.Restarting,
			"restart_scheduled":         worker.RestartScheduled,
			"breaker_open":              worker.BreakerOpen,
			"active_requests":           worker.ActiveRequests,
			"failures":                  worker.Failures,
			"restart_failures":          worker.RestartFailures,
			"quarantine_remaining":      positiveSeconds(worker.QuarantineUntil.Sub(now)),
			"restart_backoff_remaining": positiveSeconds(worker.NextRestartAt.Sub(now)),
		})
	}
	status := "degraded"
	if len(healthy) > 0 {
		status = "healthy"
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": status, "workers": rows})
}

func (g *Gateway) handleTunnel(w http.ResponseWriter, r *http.Request) {
	workerID := extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount)
	if workerID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Invalid key"})
		return
	}

	worker := g.getPreferredOrHealthyWorker(workerID)
	if worker == nil {
		w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "Worker not available"})
		return
	}
	result := g.streamFromWorker(w, r, worker, "/tunnel")
	if !result.success && !result.wroteDirect {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Stream failed"})
	}
}

func positiveSeconds(d time.Duration) int {
	if d <= 0 {
		return 0
	}
	return int(d.Seconds())
}

func extractWorkerID(key string, workerCount int) string {
	if key == "" {
		return ""
	}
	// Support both old format (w1-uuid) and new format (w1::uuid)
	var prefix string
	if idx := strings.Index(key, "::"); idx > 0 {
		prefix = key[:idx]
	} else if idx := strings.Index(key, "-"); idx > 0 {
		prefix = key[:idx]
	} else {
		return ""
	}
	for i := 1; i <= workerCount; i++ {
		if prefix == fmt.Sprintf("w%d", i) {
			return prefix
		}
	}
	return ""
}

func (g *Gateway) isWorkerAcceptingRequests(worker *Worker) bool {
	if worker == nil {
		return false
	}
	if !worker.Healthy || worker.Restarting || worker.RestartScheduled || worker.BreakerOpen {
		return false
	}
	now := time.Now()
	if now.Before(worker.QuarantineUntil) {
		return false
	}
	if !worker.LastRateLimit.IsZero() && now.Sub(worker.LastRateLimit) <= time.Duration(g.cfg.RateLimitCooldownSeconds)*time.Second {
		return false
	}
	return true
}

func (g *Gateway) getPreferredOrHealthyWorker(preferredWorkerID string) *Worker {
	if preferredWorkerID != "" {
		if preferred := g.registry.GetWorker(preferredWorkerID); g.isWorkerAcceptingRequests(preferred) {
			return preferred
		}
	}
	workers := g.registry.GetHealthyWorkers(nil)
	if len(workers) == 0 {
		return nil
	}
	return workers[rand.Intn(len(workers))]
}

func (g *Gateway) logNoHealthyWorkersSnapshot(tried map[string]bool) {
	now := time.Now()
	workers := g.registry.WorkersSnapshot()
	parts := make([]string, 0, len(workers))

	for _, w := range workers {
		reasons := make([]string, 0, 8)
		if tried != nil && tried[w.ID] {
			reasons = append(reasons, "tried")
		}
		if !w.QuarantineUntil.IsZero() && now.Before(w.QuarantineUntil) {
			reasons = append(reasons, fmt.Sprintf("quarantine=%ds", int(time.Until(w.QuarantineUntil).Seconds())))
		}
		if !w.Healthy {
			reasons = append(reasons, "healthy=false")
		}
		if w.Restarting {
			reasons = append(reasons, "restarting")
		}
		if w.RestartScheduled {
			reasons = append(reasons, "restart_scheduled")
		}
		if w.BreakerOpen {
			reasons = append(reasons, "breaker_open")
		}
		if !w.LastRateLimit.IsZero() {
			cooldownUntil := w.LastRateLimit.Add(time.Duration(g.cfg.RateLimitCooldownSeconds) * time.Second)
			if now.Before(cooldownUntil) {
				reasons = append(reasons, fmt.Sprintf("rate_cooldown=%ds", int(time.Until(cooldownUntil).Seconds())))
			}
		}
		if len(reasons) == 0 {
			reasons = append(reasons, "eligible")
		}

		parts = append(parts, fmt.Sprintf("%s(active=%d,reasons=%s)", w.ID, w.ActiveRequests, strings.Join(reasons, "|")))
	}

	log.Printf("[diag] no healthy workers snapshot: %s", strings.Join(parts, "; "))
}

func (g *Gateway) clientIP(r *http.Request) string {
	xff := r.Header.Get("X-Forwarded-For")
	if xff != "" {
		parts := strings.Split(xff, ",")
		if len(parts) > 0 {
			first := strings.TrimSpace(parts[0])
			if first != "" {
				return first
			}
		}
	}
	remote := r.RemoteAddr
	if i := strings.LastIndex(remote, ":"); i != -1 {
		remote = remote[:i]
	}
	if remote == "" {
		return "unknown"
	}
	return remote
}

func (g *Gateway) checkRateLimit(w http.ResponseWriter, r *http.Request, limiter *SlidingWindowRateLimiter, routeName string) bool {
	clientIP := g.clientIP(r)
	allowed, retryAfter := limiter.Check(clientIP)
	if allowed {
		return true
	}
	log.Printf("[ratelimit] %s blocked for %s; retry_after=%ds", routeName, clientIP, retryAfter)
	w.Header().Set("Retry-After", strconv.Itoa(retryAfter))
	writeJSON(w, http.StatusTooManyRequests, map[string]string{
		"error":  "Too Many Requests",
		"detail": fmt.Sprintf("Rate limit exceeded on %s", routeName),
	})
	return false
}

func (g *Gateway) scheduleWorkerRestart(workerID string, rateLimited bool) {
	g.registry.OpenCircuit(workerID)
	if rateLimited {
		g.registry.MarkRateLimited(workerID)
	} else {
		g.registry.MarkFailure(workerID)
	}

	scheduled := g.registry.ScheduleRestart(workerID)
	if !scheduled {
		log.Printf("[%s] restart already scheduled/running; skip duplicate schedule", workerID)
		return
	}
	if !g.ensureRestartTask(workerID, true) {
		log.Printf("[%s] restart task not started now (duplicate/in backoff/quarantine)", workerID)
	}
}

func queueRestartCandidate(queued map[string]bool, workerID string, rateLimited bool) {
	prev := queued[workerID]
	queued[workerID] = prev || rateLimited
}

func (g *Gateway) flushQueuedRestarts(queued map[string]bool) {
	for workerID, rateLimited := range queued {
		g.scheduleWorkerRestart(workerID, rateLimited)
	}
}

func (g *Gateway) ensureRestartTask(workerID string, logBlocked bool) bool {
	canStart, reason, waitSeconds := g.registry.CanStartRestart(workerID)
	if !canStart {
		if logBlocked && (reason == "quarantine" || reason == "backoff") {
			log.Printf("[%s] restart blocked by %s; wait %ds", workerID, reason, waitSeconds)
		}
		return false
	}

	g.restartTasksMu.Lock()
	defer g.restartTasksMu.Unlock()
	if _, exists := g.restartTasks[workerID]; exists {
		return false
	}
	ctx, cancel := context.WithCancel(g.ctx)
	g.restartTasks[workerID] = cancel
	go func() {
		defer func() {
			g.restartTasksMu.Lock()
			delete(g.restartTasks, workerID)
			g.restartTasksMu.Unlock()
		}()
		_ = g.rotator.RestartWorker(ctx, workerID)
	}()
	return true
}

func (g *Gateway) makeWorkerRequest(ctx context.Context, worker *Worker, r *http.Request, path, method string, body []byte, timeout time.Duration) (*http.Response, error) {
	target := worker.APIURL() + path
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}

	ctxReq := ctx
	if timeout > 0 {
		var cancel context.CancelFunc
		ctxReq, cancel = context.WithTimeout(ctxReq, timeout)
		defer cancel()
	}

	var reqBody io.Reader
	if method == http.MethodPost {
		reqBody = bytes.NewReader(body)
	}

	req, err := http.NewRequestWithContext(ctxReq, method, target, reqBody)
	if err != nil {
		return nil, err
	}
	req.Header = g.buildForwardHeaders(r, method)
	return g.client.Do(req)
}

func (g *Gateway) proxyStreamingResponse(w http.ResponseWriter, r *http.Request, worker *Worker, path, method string, body []byte) proxyResult {
	timeout := 60 * time.Second
	if method == http.MethodGet {
		timeout = 0
	}
	resp, err := g.makeWorkerRequest(r.Context(), worker, r, path, method, body, timeout)
	if err != nil {
		if r.Context().Err() != nil || isClientAbortError(err) {
			return proxyResult{success: false, wroteDirect: true}
		}
		log.Printf("[%s] request error: %v", worker.ID, err)
		return proxyResult{success: false}
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusOK {
		copyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true})
		w.WriteHeader(resp.StatusCode)
		_, copyErr := io.Copy(w, resp.Body)
		if copyErr != nil {
			if !isClientAbortError(copyErr) {
				log.Printf("[%s] stream copy error: %v", worker.ID, copyErr)
			}
			return proxyResult{success: false, wroteDirect: true}
		}
		return proxyResult{success: true, wroteDirect: true}
	}

	return g.handleResponse(w, resp, worker)
}

func (g *Gateway) handleResponse(w http.ResponseWriter, resp *http.Response, worker *Worker) proxyResult {
	status := resp.StatusCode
	body, _ := io.ReadAll(resp.Body)
	text := strings.ToLower(string(body))

	isGeoRestricted := strings.Contains(text, "geo_restricted") ||
		strings.Contains(text, "not available in your region") ||
		strings.Contains(text, "not available in your country") ||
		strings.Contains(text, "region-blocked") ||
		strings.Contains(text, "georestricted") ||
		strings.Contains(text, "geoblocked")

	isIPBlocked := strings.Contains(text, "ip blocked") ||
		strings.Contains(text, "blocked by tiktok") ||
		strings.Contains(text, "captcha") ||
		strings.Contains(text, "verify you are human") ||
		strings.Contains(text, "verify that you are human") ||
		strings.Contains(text, "access denied")

	if status == http.StatusForbidden || status == http.StatusTooManyRequests {
		patterns := []string{
			"rate-limited",
			"rate limited",
			"this content isn't available, try again later",
			"session has been rate-limited",
			"too many requests",
			"sign in to confirm",
			"not a bot",
		}
		isRateLimit := false
		for _, p := range patterns {
			if strings.Contains(text, p) {
				isRateLimit = true
				break
			}
		}
		if isGeoRestricted {
			log.Printf("[%s] received %d with geo restriction; failover without restart", worker.ID, status)
			return proxyResult{success: false, status: status, shouldRestart: false}
		}
		if isRateLimit {
			log.Printf("[%s] received %d with rate-limit pattern; failover + container restart", worker.ID, status)
			return proxyResult{success: false, status: status, isRateLimit: true, shouldRestart: true}
		}
		if isIPBlocked {
			log.Printf("[%s] received %d with IP-block pattern; failover + container restart", worker.ID, status)
			return proxyResult{success: false, status: status, shouldRestart: true}
		}
	}

	if status == http.StatusBadRequest {
		copyHeader(w.Header(), resp.Header, nil)
		if ct := resp.Header.Get("Content-Type"); ct == "" {
			w.Header().Set("Content-Type", "application/json")
		}
		w.WriteHeader(status)
		_, _ = w.Write(body)
		return proxyResult{success: true, wroteDirect: true}
	}

	if status == http.StatusServiceUnavailable {
		if isGeoRestricted {
			log.Printf("[%s] received 503 GEO_RESTRICTED; failover without restart", worker.ID)
			return proxyResult{success: false, status: status, shouldRestart: false}
		}
		if isIPBlocked {
			log.Printf("[%s] received 503 with IP-block pattern; failover + container restart", worker.ID)
			return proxyResult{success: false, status: status, shouldRestart: true}
		}
	}

	if status >= 400 && status < 500 {
		copyHeader(w.Header(), resp.Header, nil)
		if ct := resp.Header.Get("Content-Type"); ct == "" {
			w.Header().Set("Content-Type", "application/json")
		}
		w.WriteHeader(status)
		_, _ = w.Write(body)
		return proxyResult{success: true, wroteDirect: true}
	}

	return proxyResult{success: false, status: status, shouldRestart: false}
}

func (g *Gateway) proxyWithRetry(w http.ResponseWriter, r *http.Request, path, method, preferredWorkerID string, strictPreferred bool) {
	tried := map[string]bool{}
	queued := map[string]bool{}
	var body []byte
	if method == http.MethodPost {
		read, err := io.ReadAll(r.Body)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Invalid body"})
			return
		}
		body = read
	}

	for attempt := 0; attempt < g.cfg.MaxRetries; attempt++ {
		var worker *Worker
		if preferredWorkerID != "" && !tried[preferredWorkerID] {
			if preferred := g.registry.GetWorker(preferredWorkerID); g.isWorkerAcceptingRequests(preferred) {
				worker = preferred
			} else {
				log.Printf("[%s] preferred worker unavailable for key-bound request; falling back to another healthy worker", preferredWorkerID)
				if strictPreferred {
					break
				}
			}
		}
		if worker == nil {
			workers := g.registry.GetHealthyWorkers(tried)
			if len(workers) == 0 {
				log.Printf("no healthy workers available")
				g.logNoHealthyWorkersSnapshot(tried)
				break
			}
			worker = workers[rand.Intn(len(workers))]
		}
		tried[worker.ID] = true

		g.registry.IncrementActive(worker.ID)
		result := g.proxyStreamingResponse(w, r, worker, path, method, body)
		g.registry.DecrementActive(worker.ID)
		if result.success {
			if len(queued) > 0 {
				g.flushQueuedRestarts(queued)
			}
			return
		}
		if result.wroteDirect {
			return
		}

		if result.shouldRestart {
			if result.isRateLimit {
				log.Printf("[%s] rate limit detected, rotating VPN...", worker.ID)
				queueRestartCandidate(queued, worker.ID, true)
			} else {
				log.Printf("[%s] retryable failure, scheduling restart", worker.ID)
				queueRestartCandidate(queued, worker.ID, false)
			}
		} else {
			log.Printf("[%s] retryable client-side failure; failover without restart", worker.ID)
		}
	}

	log.Printf("all %d attempts failed", g.cfg.MaxRetries)
	if len(queued) > 0 {
		log.Printf("flushing %d queued restart(s) after total retry failure", len(queued))
		g.flushQueuedRestarts(queued)
	}
	w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "All workers failed or busy. Please try again later.",
	})
}

func (g *Gateway) proxyWithRotation(w http.ResponseWriter, r *http.Request, path string) {
	tried := map[string]bool{}
	queued := map[string]bool{}

	for attempt := 0; attempt < g.cfg.MaxRetries; attempt++ {
		workers := g.registry.GetHealthyWorkers(tried)
		if len(workers) == 0 {
			break
		}
		worker := workers[rand.Intn(len(workers))]
		tried[worker.ID] = true
		g.registry.IncrementActive(worker.ID)

		result := g.proxyStreamingResponse(w, r, worker, path, http.MethodGet, nil)
		g.registry.DecrementActive(worker.ID)

		if result.success {
			if len(queued) > 0 {
				g.flushQueuedRestarts(queued)
			}
			return
		}
		if result.wroteDirect {
			return
		}

		if result.shouldRestart {
			if result.isRateLimit {
				log.Printf("[%s] rate limit on stream, rotating...", worker.ID)
				queueRestartCandidate(queued, worker.ID, true)
			} else {
				log.Printf("[%s] stream failure, scheduling restart", worker.ID)
				queueRestartCandidate(queued, worker.ID, false)
			}
		} else {
			log.Printf("[%s] retryable client-side stream failure; failover without restart", worker.ID)
		}
	}

	w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
	if len(queued) > 0 {
		log.Printf("flushing %d queued restart(s) after stream retry failure", len(queued))
		g.flushQueuedRestarts(queued)
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "All workers rate limited or failed",
	})
}

func (g *Gateway) proxyToWorker(w http.ResponseWriter, r *http.Request, path string) proxyResult {
	workers := g.registry.GetHealthyWorkers(nil)
	if len(workers) == 0 {
		w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No workers available"})
		return proxyResult{success: false, wroteDirect: true}
	}
	worker := workers[rand.Intn(len(workers))]
	g.registry.IncrementActive(worker.ID)
	result := g.proxyStreamingResponse(w, r, worker, path, http.MethodGet, nil)
	g.registry.DecrementActive(worker.ID)
	return result
}

func (g *Gateway) streamFromWorker(w http.ResponseWriter, r *http.Request, worker *Worker, path string) proxyResult {
	g.registry.IncrementActive(worker.ID)
	defer g.registry.DecrementActive(worker.ID)

	resp, err := g.makeWorkerRequest(r.Context(), worker, r, path, http.MethodGet, nil, 0)
	if err != nil {
		log.Printf("[%s] stream error: %v", worker.ID, err)
		return proxyResult{success: false}
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusOK {
		estimated := resp.Header.Get("estimated-content-length")
		contentLength := resp.Header.Get("content-length")
		if estimated != "" && (contentLength == "" || contentLength == "0") {
			log.Printf("[%s] possible rate limit in stream", worker.ID)
			g.scheduleWorkerRestart(worker.ID, true)
		}
	}

	copyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true, "content-length": true})
	if resp.Header.Get("Content-Type") == "" {
		w.Header().Set("Content-Type", "application/octet-stream")
	}
	w.WriteHeader(resp.StatusCode)
	_, err = io.Copy(w, resp.Body)
	if err != nil {
		if !isClientAbortError(err) {
			log.Printf("[%s] stream copy error: %v", worker.ID, err)
		}
		return proxyResult{success: false, wroteDirect: true}
	}
	return proxyResult{success: true, wroteDirect: true}
}

func (g *Gateway) restartScheduler() {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-g.ctx.Done():
			return
		case <-ticker.C:
			workers := g.registry.WorkersSnapshot()
			for _, worker := range workers {
				if worker.RestartScheduled && !worker.Restarting {
					g.ensureRestartTask(worker.ID, false)
				}
			}
		}
	}
}

func (g *Gateway) uptimeChecker() {
	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-g.ctx.Done():
			return
		case <-ticker.C:
			needing := g.registry.GetWorkersNeedingRestart()
			for _, workerID := range needing {
				waited := 0
				for !g.registry.IsWorkerIdle(workerID) && waited < 300 {
					select {
					case <-g.ctx.Done():
						return
					case <-time.After(5 * time.Second):
						waited += 5
					}
				}
				if g.registry.IsWorkerIdle(workerID) {
					log.Printf("[uptime] restarting %s after 24h", workerID)
					_ = g.registry.ScheduleRestart(workerID)
				}
			}
		}
	}
}

func (g *Gateway) healthMonitor() {
	interval := g.cfg.HealthMonitorIntervalMs
	if interval < 1000 {
		interval = 1000
	}
	ticker := time.NewTicker(time.Duration(interval) * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-g.ctx.Done():
			return
		case <-ticker.C:
			for _, worker := range g.registry.WorkersSnapshot() {
				if worker.Restarting || worker.RestartScheduled {
					g.registry.SetHealthy(worker.ID, false)
					continue
				}
				g.registry.RecordProbe(worker.ID, g.rotator.healthCheck(worker.ID))
				if g.registry.ShouldRestartUnhealthy(worker.ID) {
					log.Printf("[%s] unhealthy for %d consecutive probes; scheduling restart", worker.ID, g.cfg.UnhealthyRestartThreshold)
					_ = g.registry.ScheduleRestart(worker.ID)
				}
			}
		}
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func envInt(key string, fallback int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func envBool(key string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func loadConfig() Config {
	return Config{
		GatewayPort:               envInt("GATEWAY_PORT", 9111),
		WorkerCount:               envInt("WORKER_COUNT", 3),
		ExtractorIPCEnabled:       envBool("EXTRACTOR_IPC_ENABLED", false),
		ExtractorPythonBin:        getenvDefault("EXTRACTOR_PYTHON_BIN", "python3"),
		ExtractorWorkerPath:       getenvDefault("EXTRACTOR_WORKER_PATH", "../extractor/worker_daemon.py"),
		ExtractorTimeoutMs:        envInt("EXTRACTOR_TIMEOUT_MS", 45000),
		GluetunPassword:           getenvDefault("GLUETUN_PASSWORD", "secretpassword"),
		MaxRetries:                envInt("MAX_RETRIES", 3),
		HealthCheckTimeoutMs:      envInt("HEALTH_CHECK_TIMEOUT_MS", 8000),
		HealthMonitorIntervalMs:   envInt("HEALTH_MONITOR_INTERVAL_MS", 5000),
		HealthFailureThreshold:    envInt("HEALTH_FAILURE_THRESHOLD", 3),
		RateLimitCooldownSeconds:  envInt("RATE_LIMIT_COOLDOWN", 300),
		RestartBackoffBase:        envInt("RESTART_BACKOFF_BASE", 30),
		RestartBackoffMax:         envInt("RESTART_BACKOFF_MAX", 300),
		RestartBudgetLimit:        envInt("RESTART_BUDGET_LIMIT", 3),
		RestartBudgetWindow:       envInt("RESTART_BUDGET_WINDOW", 600),
		RestartQuarantineSeconds:  envInt("RESTART_QUARANTINE_SECONDS", 600),
		RestartBackoffJitter:      envInt("RESTART_BACKOFF_JITTER", 5),
		DegradedRetryAfter:        envInt("DEGRADED_RETRY_AFTER", 5),
		GatewayRLWindowSeconds:    envInt("GATEWAY_RL_WINDOW_SECONDS", 60),
		GatewayRLFetchLimit:       envInt("GATEWAY_RL_FETCH_LIMIT", 45),
		GatewayRLDownloadLimit:    envInt("GATEWAY_RL_DOWNLOAD_LIMIT", 45),
		DrainTimeoutSeconds:       envInt("DRAIN_TIMEOUT_SECONDS", 90),
		DrainPollIntervalMs:       envInt("DRAIN_POLL_INTERVAL_MS", 500),
		RestartStabilizeSeconds:   envInt("RESTART_STABILIZE_SECONDS", 2),
		UnhealthyRestartThreshold: envInt("UNHEALTHY_RESTART_THRESHOLD", 6),
	}
}

func getenvDefault(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func main() {
	rand.Seed(time.Now().UnixNano())
	cfg := loadConfig()
	gateway := NewGateway(cfg)
	defer gateway.Shutdown()
	go gateway.restartScheduler()
	go gateway.uptimeChecker()
	go gateway.healthMonitor()

	addr := fmt.Sprintf(":%d", cfg.GatewayPort)
	server := &http.Server{Addr: addr, Handler: gateway}
	log.Printf("starting Go gateway on port %d", cfg.GatewayPort)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("gateway failed: %v", err)
	}
}
