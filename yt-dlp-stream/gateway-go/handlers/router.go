package handlers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"gateway-go/delivery"
	"gateway-go/registry"
	"gateway-go/utils"
)

type proxyResult struct {
	success       bool
	isRateLimit   bool
	shouldRestart bool
	status        int
	wroteDirect   bool
}

const (
	restartReasonUnknown       = "unknown"
	restartReasonProbeFailure  = "probe_failure"
	restartReasonRateLimit     = "rate_limit"
	restartReasonUptime        = "uptime"
	restartReasonHealthMonitor = "health_monitor"
)

type IPCError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Status  int    `json:"status"`
}

type Extractor interface {
	ExtractInfo(workerID, url, proxy, impersonate string) (map[string]any, *IPCError, error)
	Fetch(workerID, url, proxy, impersonate string) (map[string]any, *IPCError, error)
	TikTok(workerID, url, proxy, impersonate string) (map[string]any, *IPCError, error)
	TikTokDownloadPrepare(workerID, key string, download bool) (map[string]any, *IPCError, error)
	TikTokDownloadRefresh(workerID, key string, download bool) (map[string]any, *IPCError, error)
	DownloadPrepare(workerID, key string, download bool) (map[string]any, *IPCError, error)
	DownloadRefresh(workerID, key string, download bool) (map[string]any, *IPCError, error)
	ResolveFormats(workerID, url, format, proxy, impersonate string) (map[string]any, *IPCError, error)
	Health(workerID string) error
	PickWorker(preferred string) string
	HasWorker(workerID string) bool
	InFlight(workerID string) int
}

type Handlers struct {
	Config     utils.Config
	Registry   *registry.WorkerRegistry
	Rotator    *registry.VPNRotator
	Extractor  Extractor
	Delivery   *delivery.Delivery
	Client     *http.Client
	RLFetch    *utils.SlidingWindowRateLimiter
	RLDownload *utils.SlidingWindowRateLimiter

	restartTasksMu sync.Mutex
	restartTasks   map[string]context.CancelFunc
	restartReasons map[string]string
	ctx            context.Context
	cancel         context.CancelFunc
	startedAt      time.Time
}

func New(cfg utils.Config, reg *registry.WorkerRegistry, rotator *registry.VPNRotator, ext Extractor, del *delivery.Delivery, client *http.Client) *Handlers {
	ctx, cancel := context.WithCancel(context.Background())
	return &Handlers{
		Config:         cfg,
		Registry:       reg,
		Rotator:        rotator,
		Extractor:      ext,
		Delivery:       del,
		Client:         client,
		RLFetch:        utils.NewSlidingWindowRateLimiter(cfg.GatewayRLFetchLimit, cfg.GatewayRLWindowSeconds),
		RLDownload:     utils.NewSlidingWindowRateLimiter(cfg.GatewayRLDownloadLimit, cfg.GatewayRLWindowSeconds),
		restartTasks:   map[string]context.CancelFunc{},
		restartReasons: map[string]string{},
		ctx:            ctx,
		cancel:         cancel,
		startedAt:      time.Now(),
	}
}

func (h *Handlers) Shutdown() {
	h.cancel()
}

func (h *Handlers) ServeHTTP(w http.ResponseWriter, r *http.Request) {
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
		h.handleRoot(w, r)
	case "/fetch":
		h.handleFetch(w, r)
	case "/download":
		h.handleDownload(w, r)
	case "/info":
		h.handleInfo(w, r)
	case "/stream/video", "/stream/video-chunked", "/stream/mp3", "/stream/mp3-chunked", "/stream/m4a":
		h.handleStream(w, r)
	case "/tiktok":
		h.handleTikTok(w, r)
	case "/tiktok/download":
		h.handleTikTokDownload(w, r)
	case "/health":
		h.handleHealth(w, r)
	case "/metrics":
		h.handleMetrics(w, r)
	case "/tunnel":
		h.handleTunnel(w, r)
	default:
		http.NotFound(w, r)
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func (h *Handlers) ipcReady() bool {
	return h.Config.ExtractorIPCEnabled && h.Extractor != nil
}

func (h *Handlers) requireIPCReady(w http.ResponseWriter) bool {
	if h.ipcReady() {
		return true
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Extractor IPC unavailable",
		"detail": "EXTRACTOR_IPC_ENABLED=true but extractor workers are not ready",
	})
	return false
}

func (h *Handlers) buildForwardHeaders(r *http.Request, method string) http.Header {
	hdr := make(http.Header)
	for _, key := range []string{"Accept", "Accept-Language", "User-Agent", "Range", "If-Range", "Cookie"} {
		if v := r.Header.Get(key); v != "" {
			hdr.Set(key, v)
		}
	}
	incomingXFF := r.Header.Get("X-Forwarded-For")
	remoteIP := r.RemoteAddr
	if host, _, err := net.SplitHostPort(remoteIP); err == nil {
		remoteIP = host
	} else if i := strings.LastIndex(remoteIP, ":"); i != -1 {
		remoteIP = remoteIP[:i]
	}
	if incomingXFF != "" && remoteIP != "" {
		hdr.Set("X-Forwarded-For", incomingXFF+", "+remoteIP)
	} else if incomingXFF != "" {
		hdr.Set("X-Forwarded-For", incomingXFF)
	} else if remoteIP != "" {
		hdr.Set("X-Forwarded-For", remoteIP)
	}
	proto := "http"
	if r.TLS != nil {
		proto = "https"
	}
	hdr.Set("X-Forwarded-Proto", proto)
	if method == http.MethodPost {
		ct := r.Header.Get("Content-Type")
		if ct == "" {
			ct = "application/json"
		}
		hdr.Set("Content-Type", ct)
	}
	return hdr
}

func (h *Handlers) clientIP(r *http.Request) string {
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

func (h *Handlers) checkRateLimit(w http.ResponseWriter, r *http.Request, limiter *utils.SlidingWindowRateLimiter, routeName string) bool {
	clientIP := h.clientIP(r)
	allowed, retryAfter := limiter.Check(clientIP)
	if allowed {
		return true
	}
	log.Printf("[ratelimit] %s blocked for %s; retry_after=%ds", routeName, clientIP, retryAfter)
	w.Header().Set("Retry-After", strconv.Itoa(retryAfter))
	writeJSON(w, http.StatusTooManyRequests, map[string]string{
		"error":  "Too Many Requests",
		"detail": "Rate limit exceeded on " + routeName,
	})
	return false
}

func (h *Handlers) isWorkerAcceptingRequests(worker *registry.Worker) bool {
	if worker == nil {
		return false
	}
	if !worker.Healthy || worker.Restarting || worker.RestartScheduled || worker.BreakerOpen {
		return false
	}
	now := time.Now()
	if now.Before(worker.DegradedUntil) {
		return false
	}
	if now.Before(worker.QuarantineUntil) {
		return false
	}
	if !worker.LastRateLimit.IsZero() && now.Sub(worker.LastRateLimit) <= time.Duration(h.Config.RateLimitCooldownSeconds)*time.Second {
		return false
	}
	return true
}

func (h *Handlers) getPreferredOrHealthyWorker(preferredWorkerID string) *registry.Worker {
	if preferredWorkerID != "" {
		if preferred := h.Registry.GetWorker(preferredWorkerID); h.isWorkerAcceptingRequests(preferred) {
			return preferred
		}
	}
	workers := h.Registry.GetHealthyWorkers(nil)
	if len(workers) == 0 {
		return nil
	}
	return workers[rand.Intn(len(workers))]
}

func (h *Handlers) selectExtractorWorker(preferred string, requireHealthy bool) string {
	return h.selectExtractorWorkerAvoiding(preferred, requireHealthy, nil)
}

// selectExtractorWorkerAvoiding behaves like selectExtractorWorker but skips
// any worker IDs present in the `exclude` set. Used by retry/failover paths
// so that a worker that just returned a network error isn't immediately
// re-tried.
func (h *Handlers) selectExtractorWorkerAvoiding(preferred string, requireHealthy bool, exclude map[string]bool) string {
	if h.Extractor == nil {
		return ""
	}
	isExcluded := func(id string) bool {
		return exclude != nil && exclude[id]
	}
	if preferred != "" && !isExcluded(preferred) && h.Extractor.HasWorker(preferred) {
		if !requireHealthy {
			return preferred
		}
		if pw := h.Registry.GetWorker(preferred); h.isWorkerAcceptingRequests(pw) {
			return preferred
		}
	}
	filter := func(ids []string) []string {
		if exclude == nil {
			return ids
		}
		out := make([]string, 0, len(ids))
		for _, id := range ids {
			if !isExcluded(id) {
				out = append(out, id)
			}
		}
		return out
	}
	if requireHealthy {
		healthy := h.Registry.GetHealthyWorkers(nil)
		candidates := make([]string, 0, len(healthy))
		for _, worker := range healthy {
			if h.Extractor.HasWorker(worker.ID) {
				candidates = append(candidates, worker.ID)
			}
		}
		candidates = filter(candidates)
		if len(candidates) > 0 {
			return h.pickLeastLoadedExtractorWorker(candidates)
		}
	}
	candidates := make([]string, 0, h.Config.WorkerCount)
	for i := 1; i <= h.Config.WorkerCount; i++ {
		workerID := "w" + strconv.Itoa(i)
		if h.Extractor.HasWorker(workerID) {
			candidates = append(candidates, workerID)
		}
	}
	candidates = filter(candidates)
	if len(candidates) > 0 {
		return h.pickLeastLoadedExtractorWorker(candidates)
	}
	if exclude == nil {
		return h.Extractor.PickWorker("")
	}
	return ""
}

func (h *Handlers) pickLeastLoadedExtractorWorker(candidates []string) string {
	if len(candidates) == 0 {
		return ""
	}
	maxConcurrency := h.Config.MaxWorkerIPCConcurrency
	bestLoad := int(^uint(0) >> 1)
	best := make([]string, 0, len(candidates))
	busyBestLoad := int(^uint(0) >> 1)
	busyBest := make([]string, 0, len(candidates))
	for _, workerID := range candidates {
		load := h.Extractor.InFlight(workerID)
		if load < 0 {
			load = 0
		}
		if maxConcurrency > 0 && load >= maxConcurrency {
			switch {
			case load < busyBestLoad:
				busyBestLoad = load
				busyBest = []string{workerID}
			case load == busyBestLoad:
				busyBest = append(busyBest, workerID)
			}
			continue
		}
		switch {
		case load < bestLoad:
			bestLoad = load
			best = []string{workerID}
		case load == bestLoad:
			best = append(best, workerID)
		}
	}
	if len(best) > 0 {
		return best[rand.Intn(len(best))]
	}
	if len(busyBest) > 0 {
		return busyBest[rand.Intn(len(busyBest))]
	}
	return candidates[rand.Intn(len(candidates))]
}

func normalizeRestartReasonCode(reasonCode string) string {
	code := strings.TrimSpace(strings.ToLower(reasonCode))
	if code == "" {
		return restartReasonUnknown
	}
	return code
}

func restartReasonPriority(reasonCode string) int {
	switch normalizeRestartReasonCode(reasonCode) {
	case restartReasonRateLimit:
		return 3
	case restartReasonProbeFailure:
		return 2
	case restartReasonHealthMonitor:
		return 2
	case restartReasonUptime:
		return 1
	default:
		return 0
	}
}

func (h *Handlers) scheduleWorkerRestart(workerID string, rateLimited bool) {
	reasonCode := restartReasonProbeFailure
	if rateLimited {
		reasonCode = restartReasonRateLimit
	}
	h.scheduleWorkerRestartWithReason(workerID, reasonCode, true)
}

func (h *Handlers) scheduleWorkerRestartWithReason(workerID string, reasonCode string, markFailure bool) {
	reasonCode = normalizeRestartReasonCode(reasonCode)
	h.Registry.OpenCircuit(workerID)
	if markFailure {
		if reasonCode == restartReasonRateLimit {
			h.Registry.MarkRateLimited(workerID)
		} else {
			h.Registry.MarkFailure(workerID)
		}
	}
	scheduled := h.Registry.ScheduleRestart(workerID)
	h.restartTasksMu.Lock()
	if existing := h.restartReasons[workerID]; existing == "" || restartReasonPriority(reasonCode) > restartReasonPriority(existing) {
		h.restartReasons[workerID] = reasonCode
	}
	h.restartTasksMu.Unlock()
	if !scheduled {
		if !h.ensureRestartTask(workerID, false) {
			return
		}
		log.Printf("[%s] restart scheduled while already pending; reason=%s", workerID, reasonCode)
		return
	}
	if !h.ensureRestartTask(workerID, true) {
		log.Printf("[%s] restart task not started now (duplicate/in backoff/quarantine)", workerID)
	}
}

func queueRestartCandidate(queued map[string]string, workerID string, reasonCode string) {
	reasonCode = normalizeRestartReasonCode(reasonCode)
	prev := queued[workerID]
	if prev == "" || restartReasonPriority(reasonCode) > restartReasonPriority(prev) {
		queued[workerID] = reasonCode
	}
}

func (h *Handlers) flushQueuedRestarts(queued map[string]string) {
	for workerID, reasonCode := range queued {
		h.scheduleWorkerRestartWithReason(workerID, reasonCode, true)
	}
}

func (h *Handlers) ensureRestartTask(workerID string, logBlocked bool) bool {
	canStart, reason, waitSeconds := h.Registry.CanStartRestart(workerID)
	if !canStart {
		if logBlocked && (reason == "quarantine" || reason == "backoff") {
			log.Printf("[%s] restart blocked by %s; wait %ds", workerID, reason, waitSeconds)
		}
		return false
	}
	h.restartTasksMu.Lock()
	defer h.restartTasksMu.Unlock()
	if _, exists := h.restartTasks[workerID]; exists {
		return false
	}
	reasonCode := normalizeRestartReasonCode(h.restartReasons[workerID])
	ctx, cancel := context.WithCancel(h.ctx)
	h.restartTasks[workerID] = cancel
	go func() {
		defer func() {
			h.restartTasksMu.Lock()
			delete(h.restartTasks, workerID)
			delete(h.restartReasons, workerID)
			h.restartTasksMu.Unlock()
		}()
		_ = h.Rotator.RestartWorker(ctx, workerID, reasonCode)
	}()
	return true
}

func (h *Handlers) makeWorkerRequest(ctx context.Context, worker *registry.Worker, r *http.Request, path, method string, body []byte, timeout time.Duration) (*http.Response, error) {
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
	req.Header = h.buildForwardHeaders(r, method)
	return h.Client.Do(req)
}

func (h *Handlers) proxyStreamingResponse(w http.ResponseWriter, r *http.Request, worker *registry.Worker, path, method string, body []byte) proxyResult {
	timeout := 60 * time.Second
	if method == http.MethodGet {
		timeout = 0
	}
	resp, err := h.makeWorkerRequest(r.Context(), worker, r, path, method, body, timeout)
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
		_, copyErr := io.CopyBuffer(w, resp.Body, make([]byte, 256*1024))
		if copyErr != nil {
			if !isClientAbortError(copyErr) {
				log.Printf("[%s] stream copy error: %v", worker.ID, copyErr)
			}
			return proxyResult{success: false, wroteDirect: true}
		}
		return proxyResult{success: true, wroteDirect: true}
	}
	return h.handleResponse(w, resp, worker)
}

func (h *Handlers) handleResponse(w http.ResponseWriter, resp *http.Response, worker *registry.Worker) proxyResult {
	status := resp.StatusCode
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
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
			"rate-limited", "rate limited",
			"this content isn't available, try again later",
			"session has been rate-limited", "too many requests",
			"sign in to confirm", "not a bot",
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

func (h *Handlers) proxyWithRetry(w http.ResponseWriter, r *http.Request, path, method, preferredWorkerID string, strictPreferred bool) {
	tried := map[string]bool{}
	queued := map[string]string{}
	var body []byte
	if method == http.MethodPost {
		read, err := io.ReadAll(r.Body)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Invalid body"})
			return
		}
		body = read
	}
	for attempt := 0; attempt < h.Config.MaxRetries; attempt++ {
		var worker *registry.Worker
		if preferredWorkerID != "" && !tried[preferredWorkerID] {
			if preferred := h.Registry.GetWorker(preferredWorkerID); h.isWorkerAcceptingRequests(preferred) {
				worker = preferred
			} else {
				log.Printf("[%s] preferred worker unavailable for key-bound request; falling back to another healthy worker", preferredWorkerID)
				if strictPreferred {
					break
				}
			}
		}
		if worker == nil {
			workers := h.Registry.GetHealthyWorkers(tried)
			if len(workers) == 0 {
				log.Printf("no healthy workers available")
				h.logNoHealthyWorkersSnapshot(tried)
				break
			}
			worker = workers[rand.Intn(len(workers))]
		}
		tried[worker.ID] = true
		h.Registry.IncrementActive(worker.ID)
		result := h.proxyStreamingResponse(w, r, worker, path, method, body)
		h.Registry.DecrementActive(worker.ID)
		if result.success {
			if len(queued) > 0 {
				h.flushQueuedRestarts(queued)
			}
			return
		}
		if result.wroteDirect {
			return
		}
		if result.shouldRestart {
			if result.isRateLimit {
				log.Printf("[%s] rate limit detected, rotating VPN...", worker.ID)
				queueRestartCandidate(queued, worker.ID, restartReasonRateLimit)
			} else {
				log.Printf("[%s] retryable failure, scheduling restart", worker.ID)
				queueRestartCandidate(queued, worker.ID, restartReasonProbeFailure)
			}
		} else {
			log.Printf("[%s] retryable client-side failure; failover without restart", worker.ID)
		}
	}
	log.Printf("all %d attempts failed", h.Config.MaxRetries)
	if len(queued) > 0 {
		log.Printf("flushing %d queued restart(s) after total retry failure", len(queued))
		h.flushQueuedRestarts(queued)
	}
	w.Header().Set("Retry-After", strconv.Itoa(h.Config.DegradedRetryAfter))
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "All workers failed or busy. Please try again later.",
	})
}

func (h *Handlers) proxyWithRotation(w http.ResponseWriter, r *http.Request, path string) {
	tried := map[string]bool{}
	queued := map[string]string{}
	for attempt := 0; attempt < h.Config.MaxRetries; attempt++ {
		workers := h.Registry.GetHealthyWorkers(tried)
		if len(workers) == 0 {
			break
		}
		worker := workers[rand.Intn(len(workers))]
		tried[worker.ID] = true
		h.Registry.IncrementActive(worker.ID)
		result := h.proxyStreamingResponse(w, r, worker, path, http.MethodGet, nil)
		h.Registry.DecrementActive(worker.ID)
		if result.success {
			if len(queued) > 0 {
				h.flushQueuedRestarts(queued)
			}
			return
		}
		if result.wroteDirect {
			return
		}
		if result.shouldRestart {
			if result.isRateLimit {
				log.Printf("[%s] rate limit on stream, rotating...", worker.ID)
				queueRestartCandidate(queued, worker.ID, restartReasonRateLimit)
			} else {
				log.Printf("[%s] stream failure, scheduling restart", worker.ID)
				queueRestartCandidate(queued, worker.ID, restartReasonProbeFailure)
			}
		} else {
			log.Printf("[%s] retryable client-side stream failure; failover without restart", worker.ID)
		}
	}
	w.Header().Set("Retry-After", strconv.Itoa(h.Config.DegradedRetryAfter))
	if len(queued) > 0 {
		log.Printf("flushing %d queued restart(s) after stream retry failure", len(queued))
		h.flushQueuedRestarts(queued)
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "All workers rate limited or failed",
	})
}

func (h *Handlers) proxyToWorker(w http.ResponseWriter, r *http.Request, path string) proxyResult {
	workers := h.Registry.GetHealthyWorkers(nil)
	if len(workers) == 0 {
		w.Header().Set("Retry-After", strconv.Itoa(h.Config.DegradedRetryAfter))
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No workers available"})
		return proxyResult{success: false, wroteDirect: true}
	}
	worker := workers[rand.Intn(len(workers))]
	h.Registry.IncrementActive(worker.ID)
	result := h.proxyStreamingResponse(w, r, worker, path, http.MethodGet, nil)
	h.Registry.DecrementActive(worker.ID)
	return result
}

func (h *Handlers) streamFromWorker(w http.ResponseWriter, r *http.Request, worker *registry.Worker, path string) proxyResult {
	h.Registry.IncrementActive(worker.ID)
	defer h.Registry.DecrementActive(worker.ID)
	resp, err := h.makeWorkerRequest(r.Context(), worker, r, path, http.MethodGet, nil, 0)
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
			h.scheduleWorkerRestart(worker.ID, true)
		}
	}
	copyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true, "content-length": true})
	if resp.Header.Get("Content-Type") == "" {
		w.Header().Set("Content-Type", "application/octet-stream")
	}
	w.WriteHeader(resp.StatusCode)
	_, err = io.CopyBuffer(w, resp.Body, make([]byte, 256*1024))
	if err != nil {
		if !isClientAbortError(err) {
			log.Printf("[%s] stream copy error: %v", worker.ID, err)
		}
		return proxyResult{success: false, wroteDirect: true}
	}
	return proxyResult{success: true, wroteDirect: true}
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
		strings.Contains(msg, "context canceled") ||
		strings.Contains(msg, "use of closed network connection") ||
		strings.Contains(msg, "unexpected eof") ||
		strings.Contains(msg, "http2: server sent goaway") ||
		strings.Contains(msg, "client disconnected") ||
		strings.Contains(msg, "tls: error decoding message") ||
		strings.Contains(msg, "stream closed")
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

func positiveSeconds(d time.Duration) int {
	if d <= 0 {
		return 0
	}
	return int(d.Seconds())
}

// Goroutines (scheduler, uptime checker, health monitor)
func (h *Handlers) RestartScheduler() {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-h.ctx.Done():
			return
		case <-ticker.C:
			workers := h.Registry.WorkersSnapshot()
			for _, worker := range workers {
				if worker.RestartScheduled && !worker.Restarting {
					h.ensureRestartTask(worker.ID, false)
				}
			}
		}
	}
}

func (h *Handlers) UptimeChecker() {
	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()
	// pendingWatchers tracks workers that already have an active idle-wait
	// goroutine to prevent spawning duplicates on each 60s tick.
	var pendingWatchers sync.Map
	for {
		select {
		case <-h.ctx.Done():
			return
		case <-ticker.C:
			needing := h.Registry.GetWorkersNeedingRestart()
			for _, workerID := range needing {
				wid := workerID
				if _, loaded := pendingWatchers.LoadOrStore(wid, struct{}{}); loaded {
					continue // watcher already running for this worker
				}
				go func() {
					defer pendingWatchers.Delete(wid)
					waited := 0
					for !h.Registry.IsWorkerIdle(wid) && waited < 300 {
						select {
						case <-h.ctx.Done():
							return
						case <-time.After(5 * time.Second):
							waited += 5
						}
					}
					if h.Registry.IsWorkerIdle(wid) {
						log.Printf("[uptime] restarting %s after 24h", wid)
						h.scheduleWorkerRestartWithReason(wid, restartReasonUptime, false)
					}
				}()
			}
		}
	}
}

const workerStartupGracePeriod = 10 * time.Second

func (h *Handlers) HealthMonitor() {
	interval := h.Config.HealthMonitorIntervalMs
	if interval < 1000 {
		interval = 1000
	}
	ticker := time.NewTicker(time.Duration(interval) * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-h.ctx.Done():
			return
		case <-ticker.C:
			now := time.Now()
			workers := h.Registry.WorkersSnapshot()
			var wg sync.WaitGroup
			for _, worker := range workers {
				worker := worker
				if worker.Restarting || worker.RestartScheduled {
					h.Registry.SetHealthy(worker.ID, false)
					continue
				}
				if !worker.DegradedUntil.IsZero() && now.Before(worker.DegradedUntil) {
					continue
				}
				wg.Add(1)
				go func() {
					defer wg.Done()
					h.Registry.RecordProbe(worker.ID, h.healthCheckWorker(worker))
					if h.Registry.ShouldRestartUnhealthy(worker.ID) {
						log.Printf("[%s] unhealthy for %d consecutive probes; scheduling restart", worker.ID, h.Config.UnhealthyRestartThreshold)
						h.scheduleWorkerRestartWithReason(worker.ID, restartReasonHealthMonitor, false)
					}
				}()
			}
			wg.Wait()
		}
	}
}

func (h *Handlers) healthCheckWorker(worker registry.Worker) bool {
	// Gateway startup grace — skip probing until all workers have had a
	// chance to initialise on first boot.
	if time.Since(h.startedAt) < 60*time.Second {
		return true
	}
	// Per-worker grace after a restart — StartedAt is reset by MarkRestarted.
	if time.Since(worker.StartedAt) < workerStartupGracePeriod {
		return true
	}
	// IPC health check: asks the Python yt-dlp process directly so that
	// rate-limited or hung workers are detected, not just container liveness.
	if h.Extractor != nil {
		if err := h.Extractor.Health(worker.ID); err != nil {
			log.Printf("[%s] worker IPC health check error: %v", worker.ID, err)
			return false
		}
		return true
	}
	return h.Rotator.HealthCheck(worker.ID)
}

func extractWorkerID(key string, workerCount int) string {
	if key == "" {
		return ""
	}
	var prefix string
	if idx := strings.Index(key, "::"); idx > 0 {
		prefix = key[:idx]
	} else if idx := strings.Index(key, "-"); idx > 0 {
		prefix = key[:idx]
	} else {
		return ""
	}
	for i := 1; i <= workerCount; i++ {
		if prefix == "w"+strconv.Itoa(i) {
			return prefix
		}
	}
	return ""
}
