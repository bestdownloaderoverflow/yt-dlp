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
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const uptimeRestartInterval = 24 * time.Hour

type Config struct {
	GatewayPort                  int
	WorkerCount                  int
	WorkerHostPrefix             string
	WorkerContainerPrefix        string
	WorkerAPIPort                int
	ProxyCount                   int
	ProxyHostPrefix              string
	ProxyContainerPrefix         string
	ProxyHTTPPort                int
	ProxyControlPort             int
	MaxActivePerProxy            int
	MaxActivePerWorker           int
	WorkerPickStrategy           string
	GluetunPassword              string
	MaxRetries                   int
	HealthCheckTimeoutMs         int
	HealthMonitorIntervalMs      int
	ProxyHealthMonitorIntervalMs int
	HealthFailureThreshold       int
	RateLimitCooldownSeconds     int
	RestartBackoffBase           int
	RestartBackoffMax            int
	RestartBudgetLimit           int
	RestartBudgetWindow          int
	RestartQuarantineSeconds     int
	RestartBackoffJitter         int
	DegradedRetryAfter           int
	GatewayRLWindowSeconds       int
	GatewayRLFetchLimit          int
	GatewayRLDownloadLimit       int
	DrainTimeoutSeconds          int
	DrainPollIntervalMs          int
	RestartStabilizeSeconds      int
	UnhealthyRestartThreshold    int
	GatewayReadHeaderTimeout     int
	GatewayReadTimeout           int
	GatewayWriteTimeout          int
	GatewayIdleTimeout           int
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
			Host:        fmt.Sprintf("%s%d", cfg.WorkerHostPrefix, i),
			APIPort:     cfg.WorkerAPIPort,
			ControlPort: cfg.ProxyControlPort,
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
		if r.cfg.MaxActivePerWorker > 0 && w.ActiveRequests >= r.cfg.MaxActivePerWorker {
			continue
		}
		copyW := *w
		result = append(result, &copyW)
	}
	return result
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

type Proxy struct {
	ID               string
	Host             string
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

func (p *Proxy) ProxyURL() string {
	return fmt.Sprintf("http://%s:%d", p.Host, p.ProxyPort)
}

func (p *Proxy) ControlURL(password string) string {
	return fmt.Sprintf("http://admin:%s@%s:%d", password, p.Host, p.ControlPort)
}

type ProxyRegistry struct {
	mu      sync.Mutex
	proxies []*Proxy
	cfg     Config
}

func NewProxyRegistry(cfg Config) *ProxyRegistry {
	now := time.Now()
	proxies := make([]*Proxy, 0, cfg.ProxyCount)
	for i := 1; i <= cfg.ProxyCount; i++ {
		proxies = append(proxies, &Proxy{
			ID:          fmt.Sprintf("p%d", i),
			Host:        fmt.Sprintf("%s%d", cfg.ProxyHostPrefix, i),
			ProxyPort:   cfg.ProxyHTTPPort,
			ControlPort: cfg.ProxyControlPort,
			Healthy:     false,
			StartedAt:   now,
		})
	}
	log.Printf("initialized %d proxies", cfg.ProxyCount)
	return &ProxyRegistry{proxies: proxies, cfg: cfg}
}

func (r *ProxyRegistry) getProxyUnlocked(proxyID string) *Proxy {
	for _, p := range r.proxies {
		if p.ID == proxyID {
			return p
		}
	}
	return nil
}

func (r *ProxyRegistry) GetProxy(proxyID string) *Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return nil
	}
	copyP := *p
	return &copyP
}

func (r *ProxyRegistry) GetAvailableProxies(exclude map[string]bool) []*Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	result := make([]*Proxy, 0, len(r.proxies))
	for _, p := range r.proxies {
		if now.Before(p.QuarantineUntil) {
			continue
		}
		if !p.Healthy || p.Restarting || p.RestartScheduled || p.BreakerOpen {
			continue
		}
		if exclude != nil && exclude[p.ID] {
			continue
		}
		if !p.LastRateLimit.IsZero() && now.Sub(p.LastRateLimit) <= time.Duration(r.cfg.RateLimitCooldownSeconds)*time.Second {
			continue
		}
		if r.cfg.MaxActivePerProxy > 0 && p.ActiveRequests >= r.cfg.MaxActivePerProxy {
			continue
		}
		copyP := *p
		result = append(result, &copyP)
	}
	return result
}

func (r *ProxyRegistry) MarkRateLimited(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.LastRateLimit = time.Now()
		p.Failures++
		log.Printf("[%s] proxy marked as rate limited", proxyID)
	}
}

func (r *ProxyRegistry) MarkFailure(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.Failures++
		log.Printf("[%s] proxy marked as failed", proxyID)
	}
}

func (r *ProxyRegistry) ScheduleRestart(proxyID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		if !p.Restarting && !p.RestartScheduled {
			p.RestartScheduled = true
			p.BreakerOpen = true
			log.Printf("[%s] proxy restart scheduled", proxyID)
			return true
		}
	}
	return false
}

func (r *ProxyRegistry) CanStartRestart(proxyID string) (bool, string, int) {
	r.mu.Lock()
	defer r.mu.Unlock()

	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return false, "unknown_proxy", 0
	}
	now := time.Now()
	if p.Restarting {
		return false, "already_restarting", 0
	}
	if now.Before(p.QuarantineUntil) {
		return false, "quarantine", int(time.Until(p.QuarantineUntil).Seconds())
	}
	if now.Before(p.NextRestartAt) {
		return false, "backoff", int(time.Until(p.NextRestartAt).Seconds())
	}
	return true, "ok", 0
}

func (r *ProxyRegistry) UpdateRestartState(proxyID string, restarting bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.Restarting = restarting
		if restarting {
			p.Healthy = false
		}
	}
}

func (r *ProxyRegistry) MarkRestarted(proxyID string, success bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return
	}

	p.Restarting = false
	p.RestartScheduled = false
	if success {
		p.Healthy = true
		p.Failures = 0
		p.RestartFailures = 0
		p.StartedAt = time.Now()
		// Only clear LastRateLimit if cooldown has already expired.
		// If we clear it immediately after restart, the proxy becomes
		// eligible right away and gets rate-limited again in a tight loop.
		now := time.Now()
		if p.LastRateLimit.IsZero() || now.Sub(p.LastRateLimit) >= time.Duration(r.cfg.RateLimitCooldownSeconds)*time.Second {
			p.LastRateLimit = time.Time{}
		}
		p.NextRestartAt = time.Time{}
		p.QuarantineUntil = time.Time{}
		p.RestartEvents = nil
		p.BreakerOpen = false
		log.Printf("[%s] proxy restarted successfully", proxyID)
		return
	}

	failNow := time.Now()
	p.Healthy = false
	p.Failures++
	p.RestartFailures++

	exp := p.RestartFailures - 1
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
	p.NextRestartAt = failNow.Add(time.Duration(delay) * time.Second)

	windowStart := failNow.Add(-time.Duration(r.cfg.RestartBudgetWindow) * time.Second)
	filtered := p.RestartEvents[:0]
	for _, t := range p.RestartEvents {
		if t.After(windowStart) {
			filtered = append(filtered, t)
		}
	}
	p.RestartEvents = append(filtered, failNow)
	if len(p.RestartEvents) >= r.cfg.RestartBudgetLimit {
		p.QuarantineUntil = failNow.Add(time.Duration(r.cfg.RestartQuarantineSeconds) * time.Second)
		p.NextRestartAt = p.QuarantineUntil
		log.Printf("[%s] proxy entering quarantine for %ds after %d restart failures", proxyID, r.cfg.RestartQuarantineSeconds, len(p.RestartEvents))
	}

	p.RestartScheduled = true
	p.BreakerOpen = true
	log.Printf("[%s] proxy restart failed; retry after %ds", proxyID, delay)
}

func (r *ProxyRegistry) RecordProbe(proxyID string, healthy bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return
	}

	threshold := r.cfg.HealthFailureThreshold
	if threshold < 1 {
		threshold = 1
	}

	if healthy {
		p.ProbeFailures = 0
		p.Healthy = true
		p.BreakerOpen = false
		return
	}

	p.ProbeFailures++
	if p.ProbeFailures >= threshold {
		p.Healthy = false
	}
}

func (r *ProxyRegistry) ShouldRestartUnhealthy(proxyID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return false
	}

	threshold := r.cfg.UnhealthyRestartThreshold
	if threshold < 1 {
		return false
	}
	if p.Healthy || p.Restarting || p.RestartScheduled {
		return false
	}
	if p.ActiveRequests > 0 {
		return false
	}

	now := time.Now()
	if now.Before(p.QuarantineUntil) || now.Before(p.NextRestartAt) {
		return false
	}

	return p.ProbeFailures >= threshold
}

func (r *ProxyRegistry) GetProxiesNeedingRestart() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	needing := []string{}
	now := time.Now()
	for _, p := range r.proxies {
		uptime := now.Sub(p.StartedAt)
		if uptime >= uptimeRestartInterval && p.Healthy && !p.Restarting && !p.RestartScheduled {
			needing = append(needing, p.ID)
		}
	}
	return needing
}

func (r *ProxyRegistry) IsProxyIdle(proxyID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	return p != nil && p.ActiveRequests == 0
}

func (r *ProxyRegistry) ProxyActiveRequests(proxyID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return 0
	}
	return p.ActiveRequests
}

func (r *ProxyRegistry) IncrementActive(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.ActiveRequests++
	}
}

func (r *ProxyRegistry) DecrementActive(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil && p.ActiveRequests > 0 {
		p.ActiveRequests--
	}
}

func (r *ProxyRegistry) ProxiesSnapshot() []Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Proxy, 0, len(r.proxies))
	for _, p := range r.proxies {
		out = append(out, *p)
	}
	return out
}

type VPNRotator struct {
	workerRegistry *WorkerRegistry
	proxyRegistry  *ProxyRegistry
	cfg            Config
	healthCl       *http.Client
}

func NewVPNRotator(workerRegistry *WorkerRegistry, proxyRegistry *ProxyRegistry, cfg Config) *VPNRotator {
	timeout := cfg.HealthCheckTimeoutMs
	if timeout < 1000 {
		timeout = 1000
	}
	return &VPNRotator{
		workerRegistry: workerRegistry,
		proxyRegistry:  proxyRegistry,
		cfg:            cfg,
		healthCl:       &http.Client{Timeout: time.Duration(timeout) * time.Millisecond},
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

func (v *VPNRotator) waitForProxyDrain(ctx context.Context, proxyID string) bool {
	timeout := time.Duration(v.cfg.DrainTimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	interval := time.Duration(v.cfg.DrainPollIntervalMs) * time.Millisecond
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	deadline := time.Now().Add(timeout)
	for {
		if v.proxyRegistry.IsProxyIdle(proxyID) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(interval):
			active := v.proxyRegistry.ProxyActiveRequests(proxyID)
			log.Printf("[%s] proxy drain wait: active_requests=%d", proxyID, active)
		}
	}
}

func (v *VPNRotator) RestartProxy(ctx context.Context, proxyID string) bool {
	drained := v.waitForProxyDrain(ctx, proxyID)
	if !drained {
		log.Printf("[%s] proxy drain timeout reached; forcing container restart", proxyID)
	}

	v.proxyRegistry.UpdateRestartState(proxyID, true)
	container := fmt.Sprintf("%s%s", v.cfg.ProxyContainerPrefix, strings.TrimPrefix(proxyID, "p"))
	log.Printf("[%s] restarting %s", proxyID, container)
	if err := v.restartContainer(container); err != nil {
		log.Printf("[%s] restart error: %v", proxyID, err)
		v.proxyRegistry.MarkRestarted(proxyID, false)
		return false
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(5 * time.Second):
	}

	healthy := false
	for i := 0; i < 6; i++ {
		if v.healthCheckProxy(proxyID) {
			healthy = true
			break
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(3 * time.Second):
		}
	}
	if healthy {
		v.proxyRegistry.MarkRestarted(proxyID, true)
		return true
	}
	v.proxyRegistry.MarkRestarted(proxyID, false)
	return false
}

func (v *VPNRotator) healthCheck(workerID string) bool {
	worker := v.workerRegistry.GetWorker(workerID)
	if worker == nil {
		return false
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
		return true
	}
	return false
}

func (v *VPNRotator) healthCheckProxy(proxyID string) bool {
	proxy := v.proxyRegistry.GetProxy(proxyID)
	if proxy == nil {
		return false
	}
	ipURL := fmt.Sprintf("%s/v1/publicip/ip", proxy.ControlURL(v.cfg.GluetunPassword))
	resp, err := v.healthCl.Get(ipURL)
	if err != nil {
		log.Printf("[%s] proxy health check error: %v", proxyID, err)
		return false
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil || resp.StatusCode != http.StatusOK {
		return false
	}
	// Verify we got a non-empty IP (ensures VPN tunnel is actually connected,
	// not just Gluetun API responding while tunnel is down)
	ip := strings.TrimSpace(string(body))
	if ip == "" || ip == "0.0.0.0" {
		log.Printf("[%s] proxy returned empty/invalid VPN IP: %q", proxyID, ip)
		return false
	}
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

func (l *SlidingWindowRateLimiter) pruneStale() {
	l.mu.Lock()
	defer l.mu.Unlock()
	cutoff := time.Now().Add(-time.Duration(l.windowSeconds) * time.Second)
	for key, q := range l.events {
		idx := 0
		for idx < len(q) && (q[idx].Before(cutoff) || q[idx].Equal(cutoff)) {
			idx++
		}
		if idx == len(q) {
			delete(l.events, key)
		} else if idx > 0 {
			l.events[key] = q[idx:]
		}
	}
}

type proxyResult struct {
	success       bool
	isRateLimit   bool
	shouldRestart bool
	status        int
	wroteDirect   bool
	lastBody      []byte
	lastHeaders   http.Header
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
	cfg               Config
	registry          *WorkerRegistry
	proxyRegistry     *ProxyRegistry
	rotator           *VPNRotator
	client            *http.Client
	rlFetch           *SlidingWindowRateLimiter
	rlDownload        *SlidingWindowRateLimiter
	proxyTasksMu      sync.Mutex
	proxyRestartTasks map[string]context.CancelFunc
	ctx               context.Context
	cancel            context.CancelFunc
}

func NewGateway(cfg Config) *Gateway {
	tr := &http.Transport{
		MaxIdleConns:          512,
		MaxIdleConnsPerHost:   128,
		IdleConnTimeout:       90 * time.Second,
		DisableCompression:    false,
		ResponseHeaderTimeout: 30 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}
	ctx, cancel := context.WithCancel(context.Background())
	registry := NewWorkerRegistry(cfg)
	proxyRegistry := NewProxyRegistry(cfg)
	return &Gateway{
		cfg:               cfg,
		registry:          registry,
		proxyRegistry:     proxyRegistry,
		rotator:           NewVPNRotator(registry, proxyRegistry, cfg),
		client:            &http.Client{Transport: tr},
		rlFetch:           NewSlidingWindowRateLimiter(cfg.GatewayRLFetchLimit, cfg.GatewayRLWindowSeconds),
		rlDownload:        NewSlidingWindowRateLimiter(cfg.GatewayRLDownloadLimit, cfg.GatewayRLWindowSeconds),
		proxyRestartTasks: map[string]context.CancelFunc{},
		ctx:               ctx,
		cancel:            cancel,
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
	g.proxyWithRetry(w, r, "/fetch", http.MethodGet, "", "", false)
}

func (g *Gateway) handleDownload(w http.ResponseWriter, r *http.Request) {
	if !g.checkRateLimit(w, r, g.rlDownload, "download") {
		return
	}
	g.proxyWithRetry(
		w,
		r,
		"/download",
		http.MethodGet,
		extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount),
		extractProxyID(r.URL.Query().Get("key"), g.cfg.ProxyCount),
		false,
	)
}

func (g *Gateway) handleInfo(w http.ResponseWriter, r *http.Request) {
	g.proxyWithRetry(w, r, "/info", http.MethodGet, "", "", false)
}

func (g *Gateway) handleStream(w http.ResponseWriter, r *http.Request) {
	g.proxyWithRotation(w, r, r.URL.Path)
}

func (g *Gateway) handleTikTok(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Method not allowed"})
		return
	}
	g.proxyWithRetry(w, r, "/tiktok", http.MethodPost, "", "", false)
}

func (g *Gateway) handleTikTokDownload(w http.ResponseWriter, r *http.Request) {
	if !g.checkRateLimit(w, r, g.rlDownload, "tiktok_download") {
		return
	}
	g.proxyWithRetry(
		w,
		r,
		"/tiktok/download",
		http.MethodGet,
		extractWorkerID(r.URL.Query().Get("key"), g.cfg.WorkerCount),
		extractProxyID(r.URL.Query().Get("key"), g.cfg.ProxyCount),
		false,
	)
}

func (g *Gateway) handleHealth(w http.ResponseWriter, _ *http.Request) {
	healthy := g.registry.GetHealthyWorkers(nil)
	healthyProxies := g.proxyRegistry.GetAvailableProxies(nil)
	now := time.Now()
	workers := g.registry.WorkersSnapshot()
	proxies := g.proxyRegistry.ProxiesSnapshot()
	rows := make([]map[string]any, 0, len(workers))
	proxyRows := make([]map[string]any, 0, len(proxies))
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
	for _, proxy := range proxies {
		proxyRows = append(proxyRows, map[string]any{
			"id":                        proxy.ID,
			"healthy":                   proxy.Healthy,
			"restarting":                proxy.Restarting,
			"restart_scheduled":         proxy.RestartScheduled,
			"breaker_open":              proxy.BreakerOpen,
			"active_requests":           proxy.ActiveRequests,
			"failures":                  proxy.Failures,
			"restart_failures":          proxy.RestartFailures,
			"quarantine_remaining":      positiveSeconds(proxy.QuarantineUntil.Sub(now)),
			"restart_backoff_remaining": positiveSeconds(proxy.NextRestartAt.Sub(now)),
		})
	}
	status := "down"
	httpStatus := http.StatusServiceUnavailable
	if len(healthy) > 0 && len(healthyProxies) > 0 {
		status = "healthy"
		httpStatus = http.StatusOK
	} else if len(healthy) > 0 {
		status = "degraded_proxy"
		httpStatus = http.StatusOK
	} else if len(healthyProxies) > 0 {
		status = "degraded_worker"
		httpStatus = http.StatusServiceUnavailable
	}
	writeJSON(w, httpStatus, map[string]any{
		"status":          status,
		"healthy_workers": len(healthy),
		"healthy_proxies": len(healthyProxies),
		"workers":         rows,
		"proxies":         proxyRows,
	})
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
	for i := 1; i <= workerCount; i++ {
		if parts[0] == fmt.Sprintf("w%d", i) {
			return parts[0]
		}
	}
	return ""
}

func extractProxyID(key string, proxyCount int) string {
	if key == "" {
		return ""
	}
	parts := strings.Split(key, "-")
	for i := 1; i <= proxyCount; i++ {
		if parts[0] == fmt.Sprintf("p%d", i) {
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
	if g.cfg.MaxActivePerWorker > 0 && worker.ActiveRequests >= g.cfg.MaxActivePerWorker {
		return false
	}
	return true
}

func (g *Gateway) isProxyAcceptingRequests(proxy *Proxy) bool {
	if proxy == nil {
		return false
	}
	if !proxy.Healthy || proxy.Restarting || proxy.RestartScheduled || proxy.BreakerOpen {
		return false
	}
	now := time.Now()
	if now.Before(proxy.QuarantineUntil) {
		return false
	}
	if !proxy.LastRateLimit.IsZero() && now.Sub(proxy.LastRateLimit) <= time.Duration(g.cfg.RateLimitCooldownSeconds)*time.Second {
		return false
	}
	if g.cfg.MaxActivePerProxy > 0 && proxy.ActiveRequests >= g.cfg.MaxActivePerProxy {
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
	return g.selectWorker(workers)
}

func (g *Gateway) selectWorker(workers []*Worker) *Worker {
	if len(workers) == 0 {
		return nil
	}

	strategy := strings.ToLower(strings.TrimSpace(g.cfg.WorkerPickStrategy))
	if strategy == "random" || len(workers) == 1 {
		return workers[rand.Intn(len(workers))]
	}

	if strategy == "least" {
		best := workers[rand.Intn(len(workers))]
		for _, w := range workers {
			if w.ActiveRequests < best.ActiveRequests {
				best = w
			}
		}
		return best
	}

	// Default p2c (power of two choices): balances load better than pure random under burst traffic.
	a := workers[rand.Intn(len(workers))]
	b := workers[rand.Intn(len(workers))]
	for b.ID == a.ID && len(workers) > 1 {
		b = workers[rand.Intn(len(workers))]
	}
	if b.ActiveRequests < a.ActiveRequests {
		return b
	}
	if b.ActiveRequests == a.ActiveRequests && b.Failures < a.Failures {
		return b
	}
	return a
}

func (g *Gateway) selectProxy(proxies []*Proxy) *Proxy {
	if len(proxies) == 0 {
		return nil
	}
	if len(proxies) == 1 {
		return proxies[0]
	}
	a := proxies[rand.Intn(len(proxies))]
	b := proxies[rand.Intn(len(proxies))]
	for b.ID == a.ID && len(proxies) > 1 {
		b = proxies[rand.Intn(len(proxies))]
	}
	if b.ActiveRequests < a.ActiveRequests {
		return b
	}
	if b.ActiveRequests == a.ActiveRequests && b.Failures < a.Failures {
		return b
	}
	return a
}

func (g *Gateway) shouldInjectProxy(path, method string) bool {
	if method == http.MethodGet {
		switch path {
		case "/fetch", "/info", "/stream/video", "/stream/video-chunked", "/stream/mp3", "/stream/mp3-chunked", "/stream/m4a",
			"/tiktok/download":
			return true
		}
	}
	return method == http.MethodPost && path == "/tiktok"
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

func (g *Gateway) logNoHealthyProxiesSnapshot(tried map[string]bool) {
	now := time.Now()
	proxies := g.proxyRegistry.ProxiesSnapshot()
	parts := make([]string, 0, len(proxies))

	for _, p := range proxies {
		reasons := make([]string, 0, 8)
		if tried != nil && tried[p.ID] {
			reasons = append(reasons, "tried")
		}
		if !p.QuarantineUntil.IsZero() && now.Before(p.QuarantineUntil) {
			reasons = append(reasons, fmt.Sprintf("quarantine=%ds", int(time.Until(p.QuarantineUntil).Seconds())))
		}
		if !p.Healthy {
			reasons = append(reasons, "healthy=false")
		}
		if p.Restarting {
			reasons = append(reasons, "restarting")
		}
		if p.RestartScheduled {
			reasons = append(reasons, "restart_scheduled")
		}
		if p.BreakerOpen {
			reasons = append(reasons, "breaker_open")
		}
		if !p.LastRateLimit.IsZero() {
			cooldownUntil := p.LastRateLimit.Add(time.Duration(g.cfg.RateLimitCooldownSeconds) * time.Second)
			if now.Before(cooldownUntil) {
				reasons = append(reasons, fmt.Sprintf("rate_cooldown=%ds", int(time.Until(cooldownUntil).Seconds())))
			}
		}
		if len(reasons) == 0 {
			reasons = append(reasons, "eligible")
		}
		parts = append(parts, fmt.Sprintf("%s(active=%d,reasons=%s)", p.ID, p.ActiveRequests, strings.Join(reasons, "|")))
	}
	log.Printf("[diag] no healthy proxies snapshot: %s", strings.Join(parts, "; "))
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

func (g *Gateway) scheduleProxyRestart(proxyID string, rateLimited bool) {
	if rateLimited {
		g.proxyRegistry.MarkRateLimited(proxyID)
	} else {
		g.proxyRegistry.MarkFailure(proxyID)
	}

	scheduled := g.proxyRegistry.ScheduleRestart(proxyID)
	if !scheduled {
		log.Printf("[%s] proxy restart already scheduled/running; skip duplicate schedule", proxyID)
		return
	}
	if !g.ensureProxyRestartTask(proxyID, true) {
		log.Printf("[%s] proxy restart task not started now (duplicate/in backoff/quarantine)", proxyID)
	}
}

func queueProxyRestartCandidate(queued map[string]bool, proxyID string, rateLimited bool) {
	prev := queued[proxyID]
	queued[proxyID] = prev || rateLimited
}

func (g *Gateway) flushQueuedProxyRestarts(queued map[string]bool) {
	for proxyID, rateLimited := range queued {
		g.scheduleProxyRestart(proxyID, rateLimited)
	}
}

func (g *Gateway) ensureProxyRestartTask(proxyID string, logBlocked bool) bool {
	canStart, reason, waitSeconds := g.proxyRegistry.CanStartRestart(proxyID)
	if !canStart {
		if logBlocked && (reason == "quarantine" || reason == "backoff") {
			log.Printf("[%s] proxy restart blocked by %s; wait %ds", proxyID, reason, waitSeconds)
		}
		return false
	}

	g.proxyTasksMu.Lock()
	defer g.proxyTasksMu.Unlock()
	if _, exists := g.proxyRestartTasks[proxyID]; exists {
		return false
	}
	ctx, cancel := context.WithCancel(g.ctx)
	g.proxyRestartTasks[proxyID] = cancel
	go func() {
		defer func() {
			g.proxyTasksMu.Lock()
			delete(g.proxyRestartTasks, proxyID)
			g.proxyTasksMu.Unlock()
		}()
		_ = g.rotator.RestartProxy(ctx, proxyID)
	}()
	return true
}

func (g *Gateway) maybeInjectProxy(path, method string, rawQuery string, body []byte, proxyURL string) (string, []byte) {
	if proxyURL == "" || !g.shouldInjectProxy(path, method) {
		return rawQuery, body
	}

	updatedBody := body
	values, err := url.ParseQuery(rawQuery)
	if err == nil {
		if strings.TrimSpace(values.Get("proxy")) == "" {
			values.Set("proxy", proxyURL)
		}
		rawQuery = values.Encode()
	}

	if method == http.MethodPost && path == "/tiktok" && len(body) > 0 {
		var payload map[string]any
		if err := json.Unmarshal(body, &payload); err == nil {
			if strings.TrimSpace(fmt.Sprintf("%v", payload["proxy"])) == "" || payload["proxy"] == nil {
				payload["proxy"] = proxyURL
				if buf, err := json.Marshal(payload); err == nil {
					updatedBody = buf
				}
			}
		}
	}
	return rawQuery, updatedBody
}

func (g *Gateway) makeWorkerRequest(ctx context.Context, worker *Worker, r *http.Request, path, method string, body []byte, timeout time.Duration, proxy *Proxy) (*http.Response, error) {
	rawQuery := r.URL.RawQuery
	updatedBody := body
	if proxy != nil {
		rawQuery, updatedBody = g.maybeInjectProxy(path, method, rawQuery, body, proxy.ProxyURL())
	}
	target := worker.APIURL() + path
	if rawQuery != "" {
		target += "?" + rawQuery
	}

	ctxReq := ctx
	if timeout > 0 {
		var cancel context.CancelFunc
		ctxReq, cancel = context.WithTimeout(ctxReq, timeout)
		defer cancel()
	}

	var reqBody io.Reader
	if method == http.MethodPost {
		reqBody = bytes.NewReader(updatedBody)
	}

	req, err := http.NewRequestWithContext(ctxReq, method, target, reqBody)
	if err != nil {
		return nil, err
	}
	req.Header = g.buildForwardHeaders(r, method)
	return g.client.Do(req)
}

func (g *Gateway) proxyStreamingResponse(w http.ResponseWriter, r *http.Request, worker *Worker, path, method string, body []byte, proxy *Proxy) proxyResult {
	timeout := 60 * time.Second
	if method == http.MethodGet {
		timeout = 0
	}
	resp, err := g.makeWorkerRequest(r.Context(), worker, r, path, method, body, timeout, proxy)
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

	isNotFound := status == http.StatusNotFound &&
		(strings.Contains(text, "video not found") ||
			strings.Contains(text, "not found") ||
			strings.Contains(text, "unable to download") ||
			strings.Contains(text, "unsupported url"))

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

	if status == http.StatusNotFound && isNotFound {
		log.Printf("[%s] received 404 video-not-found; failover to next proxy without restart", worker.ID)
		return proxyResult{success: false, status: status, shouldRestart: false, lastBody: body, lastHeaders: resp.Header}
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

	return proxyResult{success: false, status: status, shouldRestart: false, lastBody: body, lastHeaders: resp.Header}
}

func (g *Gateway) proxyWithRetry(w http.ResponseWriter, r *http.Request, path, method, preferredWorkerID, preferredProxyID string, strictPreferred bool) {
	triedWorkers := map[string]bool{}
	triedProxies := map[string]bool{}
	queuedProxies := map[string]bool{}
	var lastFailResult proxyResult
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
		if preferredWorkerID != "" && !triedWorkers[preferredWorkerID] {
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
			workers := g.registry.GetHealthyWorkers(triedWorkers)
			if len(workers) == 0 {
				log.Printf("no healthy workers available")
				g.logNoHealthyWorkersSnapshot(triedWorkers)
				break
			}
			worker = g.selectWorker(workers)
		}
		triedWorkers[worker.ID] = true

		var proxy *Proxy
		if g.shouldInjectProxy(path, method) {
			if preferredProxyID != "" && !triedProxies[preferredProxyID] {
				if preferred := g.proxyRegistry.GetProxy(preferredProxyID); g.isProxyAcceptingRequests(preferred) {
					proxy = preferred
				} else {
					log.Printf("[%s] preferred proxy unavailable for key-bound request; falling back to another healthy proxy", preferredProxyID)
				}
			}
			if proxy == nil {
				proxies := g.proxyRegistry.GetAvailableProxies(triedProxies)
				if len(proxies) == 0 {
					log.Printf("no healthy proxies available")
					g.logNoHealthyProxiesSnapshot(triedProxies)
					break
				}
				proxy = g.selectProxy(proxies)
			}
			triedProxies[proxy.ID] = true
		}

		g.registry.IncrementActive(worker.ID)
		if proxy != nil {
			g.proxyRegistry.IncrementActive(proxy.ID)
		}
		result := g.proxyStreamingResponse(w, r, worker, path, method, body, proxy)
		g.registry.DecrementActive(worker.ID)
		if proxy != nil {
			g.proxyRegistry.DecrementActive(proxy.ID)
		}
		if result.success {
			if len(queuedProxies) > 0 {
				g.flushQueuedProxyRestarts(queuedProxies)
			}
			return
		}
		if result.wroteDirect {
			return
		}
		if result.lastBody != nil {
			lastFailResult = result
		}

		if result.shouldRestart {
			if proxy != nil {
				if result.isRateLimit {
					log.Printf("[%s/%s] rate limit detected, rotating proxy...", worker.ID, proxy.ID)
					queueProxyRestartCandidate(queuedProxies, proxy.ID, true)
				} else {
					log.Printf("[%s/%s] retryable failure, scheduling proxy restart", worker.ID, proxy.ID)
					queueProxyRestartCandidate(queuedProxies, proxy.ID, false)
				}
			} else {
				if result.isRateLimit {
					log.Printf("[%s] rate limit detected on direct worker path; failover without worker restart", worker.ID)
				} else {
					log.Printf("[%s] retryable failure on direct worker path; failover without worker restart", worker.ID)
				}
			}
		} else {
			log.Printf("[%s] retryable client-side failure; failover without restart", worker.ID)
		}
	}

	log.Printf("all %d attempts failed", g.cfg.MaxRetries)
	if len(queuedProxies) > 0 {
		log.Printf("flushing %d queued proxy restart(s) after total retry failure", len(queuedProxies))
		g.flushQueuedProxyRestarts(queuedProxies)
	}
	if lastFailResult.lastBody != nil && lastFailResult.status >= 400 && lastFailResult.status < 500 {
		copyHeader(w.Header(), lastFailResult.lastHeaders, nil)
		if w.Header().Get("Content-Type") == "" {
			w.Header().Set("Content-Type", "application/json")
		}
		w.WriteHeader(lastFailResult.status)
		_, _ = w.Write(lastFailResult.lastBody)
		return
	}
	w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "All workers failed or busy. Please try again later.",
	})
}

func (g *Gateway) proxyWithRotation(w http.ResponseWriter, r *http.Request, path string) {
	triedWorkers := map[string]bool{}
	triedProxies := map[string]bool{}
	queuedProxies := map[string]bool{}

	for attempt := 0; attempt < g.cfg.MaxRetries; attempt++ {
		workers := g.registry.GetHealthyWorkers(triedWorkers)
		if len(workers) == 0 {
			break
		}
		worker := g.selectWorker(workers)
		triedWorkers[worker.ID] = true

		proxies := g.proxyRegistry.GetAvailableProxies(triedProxies)
		if len(proxies) == 0 {
			log.Printf("no healthy proxies available for stream")
			g.logNoHealthyProxiesSnapshot(triedProxies)
			break
		}
		proxy := g.selectProxy(proxies)
		triedProxies[proxy.ID] = true

		g.registry.IncrementActive(worker.ID)
		g.proxyRegistry.IncrementActive(proxy.ID)

		result := g.proxyStreamingResponse(w, r, worker, path, http.MethodGet, nil, proxy)
		g.registry.DecrementActive(worker.ID)
		g.proxyRegistry.DecrementActive(proxy.ID)

		if result.success {
			if len(queuedProxies) > 0 {
				g.flushQueuedProxyRestarts(queuedProxies)
			}
			return
		}
		if result.wroteDirect {
			return
		}

		if result.shouldRestart {
			if result.isRateLimit {
				log.Printf("[%s/%s] rate limit on stream, rotating proxy...", worker.ID, proxy.ID)
				queueProxyRestartCandidate(queuedProxies, proxy.ID, true)
			} else {
				log.Printf("[%s/%s] stream failure, scheduling proxy restart", worker.ID, proxy.ID)
				queueProxyRestartCandidate(queuedProxies, proxy.ID, false)
			}
		} else {
			log.Printf("[%s] retryable client-side stream failure; failover without restart", worker.ID)
		}
	}

	w.Header().Set("Retry-After", strconv.Itoa(g.cfg.DegradedRetryAfter))
	if len(queuedProxies) > 0 {
		log.Printf("flushing %d queued proxy restart(s) after stream retry failure", len(queuedProxies))
		g.flushQueuedProxyRestarts(queuedProxies)
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
	worker := g.selectWorker(workers)
	g.registry.IncrementActive(worker.ID)
	result := g.proxyStreamingResponse(w, r, worker, path, http.MethodGet, nil, nil)
	g.registry.DecrementActive(worker.ID)
	return result
}

func (g *Gateway) streamFromWorker(w http.ResponseWriter, r *http.Request, worker *Worker, path string) proxyResult {
	g.registry.IncrementActive(worker.ID)
	defer g.registry.DecrementActive(worker.ID)

	resp, err := g.makeWorkerRequest(r.Context(), worker, r, path, http.MethodGet, nil, 0, nil)
	if err != nil {
		log.Printf("[%s] stream error: %v", worker.ID, err)
		return proxyResult{success: false}
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusOK {
		estimated := resp.Header.Get("estimated-content-length")
		contentLength := resp.Header.Get("content-length")
		if estimated != "" && (contentLength == "" || contentLength == "0") {
			log.Printf("[%s] possible rate limit in direct stream; worker restart disabled", worker.ID)
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
			proxies := g.proxyRegistry.ProxiesSnapshot()
			for _, proxy := range proxies {
				if proxy.RestartScheduled && !proxy.Restarting {
					g.ensureProxyRestartTask(proxy.ID, false)
				}
			}
			g.rlFetch.pruneStale()
			g.rlDownload.pruneStale()
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
			needing := g.proxyRegistry.GetProxiesNeedingRestart()
			for _, proxyID := range needing {
				waited := 0
				for !g.proxyRegistry.IsProxyIdle(proxyID) && waited < 300 {
					select {
					case <-g.ctx.Done():
						return
					case <-time.After(5 * time.Second):
						waited += 5
					}
				}
				if g.proxyRegistry.IsProxyIdle(proxyID) {
					log.Printf("[uptime] restarting %s after 24h", proxyID)
					_ = g.proxyRegistry.ScheduleRestart(proxyID)
				}
			}
		}
	}
}

func (g *Gateway) healthMonitor() {
	workerInterval := g.cfg.HealthMonitorIntervalMs
	if workerInterval < 1000 {
		workerInterval = 1000
	}
	proxyInterval := g.cfg.ProxyHealthMonitorIntervalMs
	if proxyInterval < 1000 {
		proxyInterval = 1000
	}

	workerTicker := time.NewTicker(time.Duration(workerInterval) * time.Millisecond)
	proxyTicker := time.NewTicker(time.Duration(proxyInterval) * time.Millisecond)
	defer workerTicker.Stop()
	defer proxyTicker.Stop()

	log.Printf("health monitor started: workers every %dms, proxies every %dms", workerInterval, proxyInterval)

	for {
		select {
		case <-g.ctx.Done():
			return
		case <-workerTicker.C:
			for _, worker := range g.registry.WorkersSnapshot() {
				g.registry.RecordProbe(worker.ID, g.rotator.healthCheck(worker.ID))
			}
		case <-proxyTicker.C:
			for _, proxy := range g.proxyRegistry.ProxiesSnapshot() {
				if proxy.Restarting || proxy.RestartScheduled {
					continue
				}
				g.proxyRegistry.RecordProbe(proxy.ID, g.rotator.healthCheckProxy(proxy.ID))
				if g.proxyRegistry.ShouldRestartUnhealthy(proxy.ID) {
					log.Printf("[%s] proxy unhealthy for %d consecutive probes; scheduling restart", proxy.ID, g.cfg.UnhealthyRestartThreshold)
					_ = g.proxyRegistry.ScheduleRestart(proxy.ID)
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
		GatewayPort:                  envInt("GATEWAY_PORT", 9111),
		WorkerCount:                  envInt("WORKER_COUNT", 3),
		WorkerHostPrefix:             getenvDefault("WORKER_HOST_PREFIX", "ytdlp-worker-"),
		WorkerContainerPrefix:        getenvDefault("WORKER_CONTAINER_PREFIX", "ytdlp-worker-"),
		WorkerAPIPort:                envInt("WORKER_API_PORT", 9487),
		ProxyCount:                   envInt("PROXY_COUNT", 10),
		ProxyHostPrefix:              getenvDefault("PROXY_HOST_PREFIX", "gluetun-"),
		ProxyContainerPrefix:         getenvDefault("PROXY_CONTAINER_PREFIX", "ytdlp-gluetun-"),
		ProxyHTTPPort:                envInt("PROXY_HTTP_PORT", 8888),
		ProxyControlPort:             envInt("PROXY_CONTROL_PORT", 8000),
		MaxActivePerProxy:            envInt("MAX_ACTIVE_PER_PROXY", 30),
		MaxActivePerWorker:           envInt("MAX_ACTIVE_PER_WORKER", 40),
		WorkerPickStrategy:           getenvDefault("WORKER_PICK_STRATEGY", "p2c"),
		GluetunPassword:              getenvDefault("GLUETUN_PASSWORD", "secretpassword"),
		MaxRetries:                   envInt("MAX_RETRIES", 3),
		HealthCheckTimeoutMs:         envInt("HEALTH_CHECK_TIMEOUT_MS", 8000),
		HealthMonitorIntervalMs:      envInt("HEALTH_MONITOR_INTERVAL_MS", 5000),
		ProxyHealthMonitorIntervalMs: envInt("PROXY_HEALTH_MONITOR_INTERVAL_MS", 20000),
		HealthFailureThreshold:       envInt("HEALTH_FAILURE_THRESHOLD", 3),
		RateLimitCooldownSeconds:     envInt("RATE_LIMIT_COOLDOWN", 300),
		RestartBackoffBase:           envInt("RESTART_BACKOFF_BASE", 30),
		RestartBackoffMax:            envInt("RESTART_BACKOFF_MAX", 300),
		RestartBudgetLimit:           envInt("RESTART_BUDGET_LIMIT", 3),
		RestartBudgetWindow:          envInt("RESTART_BUDGET_WINDOW", 600),
		RestartQuarantineSeconds:     envInt("RESTART_QUARANTINE_SECONDS", 600),
		RestartBackoffJitter:         envInt("RESTART_BACKOFF_JITTER", 5),
		DegradedRetryAfter:           envInt("DEGRADED_RETRY_AFTER", 5),
		GatewayRLWindowSeconds:       envInt("GATEWAY_RL_WINDOW_SECONDS", 60),
		GatewayRLFetchLimit:          envInt("GATEWAY_RL_FETCH_LIMIT", 45),
		GatewayRLDownloadLimit:       envInt("GATEWAY_RL_DOWNLOAD_LIMIT", 45),
		DrainTimeoutSeconds:          envInt("DRAIN_TIMEOUT_SECONDS", 90),
		DrainPollIntervalMs:          envInt("DRAIN_POLL_INTERVAL_MS", 500),
		RestartStabilizeSeconds:      envInt("RESTART_STABILIZE_SECONDS", 2),
		UnhealthyRestartThreshold:    envInt("UNHEALTHY_RESTART_THRESHOLD", 6),
		GatewayReadHeaderTimeout:     envInt("GATEWAY_READ_HEADER_TIMEOUT_SECONDS", 10),
		GatewayReadTimeout:           envInt("GATEWAY_READ_TIMEOUT_SECONDS", 30),
		GatewayWriteTimeout:          envInt("GATEWAY_WRITE_TIMEOUT_SECONDS", 0),
		GatewayIdleTimeout:           envInt("GATEWAY_IDLE_TIMEOUT_SECONDS", 120),
	}
}

func getenvDefault(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func main() {
	cfg := loadConfig()
	gateway := NewGateway(cfg)
	go gateway.restartScheduler()
	go gateway.uptimeChecker()
	go gateway.healthMonitor()

	addr := fmt.Sprintf(":%d", cfg.GatewayPort)
	server := &http.Server{
		Addr:              addr,
		Handler:           gateway,
		ReadHeaderTimeout: time.Duration(cfg.GatewayReadHeaderTimeout) * time.Second,
		ReadTimeout:       time.Duration(cfg.GatewayReadTimeout) * time.Second,
		WriteTimeout:      time.Duration(cfg.GatewayWriteTimeout) * time.Second,
		IdleTimeout:       time.Duration(cfg.GatewayIdleTimeout) * time.Second,
	}
	if cfg.GatewayWriteTimeout <= 0 {
		server.WriteTimeout = 0
	}
	log.Printf("starting Go gateway on port %d", cfg.GatewayPort)

	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh

		shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			log.Printf("gateway graceful shutdown error: %v", err)
		}
		gateway.cancel()
	}()

	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("gateway failed: %v", err)
	}
}
