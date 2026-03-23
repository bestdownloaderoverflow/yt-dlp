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
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

const uptimeRestartInterval = 24 * time.Hour

type Config struct {
	GatewayPort              int
	WorkerCount              int
	GluetunPassword          string
	MaxRetries               int
	RateLimitCooldownSeconds int
	RestartBackoffBase       int
	RestartBackoffMax        int
	RestartBudgetLimit       int
	RestartBudgetWindow      int
	RestartQuarantineSeconds int
	RestartBackoffJitter     int
	DegradedRetryAfter       int
	GatewayRLWindowSeconds   int
	GatewayRLFetchLimit      int
	GatewayRLDownloadLimit   int
	DrainTimeoutSeconds      int
	DrainPollIntervalMs      int
	RestartStabilizeSeconds  int
}

type Worker struct {
	ID               string
	Host             string
	APIPort          int
	ControlPort      int
	Healthy          bool
	Restarting       bool
	RestartScheduled bool
	Failures         int
	RestartFailures  int
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
			ControlPort: 8000,
			Healthy:     true,
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
	return &VPNRotator{
		registry: registry,
		cfg:      cfg,
		healthCl: &http.Client{Timeout: 5 * time.Second},
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
			if resp.StatusCode == http.StatusOK {
				log.Printf("[%s] gluetun control reachable", workerID)
			}
		}
	}

	healthURL := fmt.Sprintf("%s/health", worker.APIURL())
	resp, err := v.healthCl.Get(healthURL)
	if err != nil {
		log.Printf("[%s] API health check error: %v", workerID, err)
		return false
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode == http.StatusOK {
		log.Printf("[%s] worker API healthy", workerID)
		return true
	}
	return false
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
	return &Gateway{
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
	result := g.proxyToWorker(w, r, "/")
	if !result.success && !result.wroteDirect {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Worker failed"})
	}
}

func (g *Gateway) handleFetch(w http.ResponseWriter, r *http.Request) {
	if !g.checkRateLimit(w, r, g.rlFetch, "fetch") {
		return
	}
	g.proxyWithRetry(w, r, "/fetch", http.MethodGet, "", false)
}

func (g *Gateway) handleDownload(w http.ResponseWriter, r *http.Request) {
	if !g.checkRateLimit(w, r, g.rlDownload, "download") {
		return
	}
	g.proxyWithRetry(w, r, "/download", http.MethodGet, extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount), false)
}

func (g *Gateway) handleInfo(w http.ResponseWriter, r *http.Request) {
	g.proxyWithRetry(w, r, "/info", http.MethodGet, "", false)
}

func (g *Gateway) handleStream(w http.ResponseWriter, r *http.Request) {
	g.proxyWithRotation(w, r, r.URL.Path)
}

func (g *Gateway) handleTikTok(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Method not allowed"})
		return
	}
	g.proxyWithRetry(w, r, "/tiktok", http.MethodPost, "", false)
}

func (g *Gateway) handleTikTokDownload(w http.ResponseWriter, r *http.Request) {
	g.proxyWithRetry(w, r, "/tiktok/download", http.MethodGet, extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount), false)
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
	parts := strings.Split(key, "-")
	if len(parts) == 0 {
		return ""
	}
	for i := 1; i <= workerCount; i++ {
		if parts[0] == fmt.Sprintf("w%d", i) {
			return parts[0]
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
		if isRateLimit {
			log.Printf("[%s] received %d with rate-limit pattern; failover + container restart", worker.ID, status)
		} else {
			log.Printf("[%s] received %d; failover + container restart (forbidden/rate path)", worker.ID, status)
		}
		return proxyResult{success: false, status: status, isRateLimit: true, shouldRestart: true}
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

func loadConfig() Config {
	return Config{
		GatewayPort:              envInt("GATEWAY_PORT", 9111),
		WorkerCount:              envInt("WORKER_COUNT", 3),
		GluetunPassword:          getenvDefault("GLUETUN_PASSWORD", "secretpassword"),
		MaxRetries:               envInt("MAX_RETRIES", 3),
		RateLimitCooldownSeconds: envInt("RATE_LIMIT_COOLDOWN", 300),
		RestartBackoffBase:       envInt("RESTART_BACKOFF_BASE", 30),
		RestartBackoffMax:        envInt("RESTART_BACKOFF_MAX", 300),
		RestartBudgetLimit:       envInt("RESTART_BUDGET_LIMIT", 3),
		RestartBudgetWindow:      envInt("RESTART_BUDGET_WINDOW", 600),
		RestartQuarantineSeconds: envInt("RESTART_QUARANTINE_SECONDS", 600),
		RestartBackoffJitter:     envInt("RESTART_BACKOFF_JITTER", 5),
		DegradedRetryAfter:       envInt("DEGRADED_RETRY_AFTER", 5),
		GatewayRLWindowSeconds:   envInt("GATEWAY_RL_WINDOW_SECONDS", 60),
		GatewayRLFetchLimit:      envInt("GATEWAY_RL_FETCH_LIMIT", 45),
		GatewayRLDownloadLimit:   envInt("GATEWAY_RL_DOWNLOAD_LIMIT", 45),
		DrainTimeoutSeconds:      envInt("DRAIN_TIMEOUT_SECONDS", 90),
		DrainPollIntervalMs:      envInt("DRAIN_POLL_INTERVAL_MS", 500),
		RestartStabilizeSeconds:  envInt("RESTART_STABILIZE_SECONDS", 2),
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
	go gateway.restartScheduler()
	go gateway.uptimeChecker()

	addr := fmt.Sprintf(":%d", cfg.GatewayPort)
	server := &http.Server{Addr: addr, Handler: gateway}
	log.Printf("starting Go gateway on port %d", cfg.GatewayPort)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("gateway failed: %v", err)
	}
}
