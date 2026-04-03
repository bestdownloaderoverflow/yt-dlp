package handlers

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"gateway-go/delivery"
)

// handleRoot returns status JSON.
func (h *Handlers) handleRoot(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"service":               "yt-dlp gateway",
		"transport":             "go-http + python-ipc",
		"extractor_ipc_enabled": h.Config.ExtractorIPCEnabled,
		"extractor_ipc_ready":   h.ipcReady(),
		"status":                "ok",
	})
}

// handleHealth returns worker health snapshot.
func (h *Handlers) handleHealth(w http.ResponseWriter, _ *http.Request) {
	healthy := h.Registry.GetHealthyWorkers(nil)
	now := time.Now()
	workers := h.Registry.WorkersSnapshot()
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

// handleFetch routes to IPC or proxy.
func (h *Handlers) handleFetch(w http.ResponseWriter, r *http.Request) {
	if !h.checkRateLimit(w, r, h.RLFetch, "fetch") {
		return
	}
	if h.Config.ExtractorIPCEnabled {
		if !h.requireIPCReady(w) {
			return
		}
		h.handleFetchViaExtractorIPC(w, r)
		return
	}
	h.proxyWithRetry(w, r, "/fetch", http.MethodGet, "", false)
}

func (h *Handlers) handleFetchViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("url"))
	if url == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing url query parameter"})
		return
	}
	proxy := strings.TrimSpace(r.URL.Query().Get("proxy"))
	impersonate := strings.TrimSpace(r.URL.Query().Get("impersonate"))
	preferred := extractWorkerID(r.URL.Query().Get("key"), h.Config.WorkerCount)
	result, workerID, rpcErr, err := h.callExtractorMapWithFailover(preferred, true, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.Fetch(wid, url, proxy, impersonate)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// handleInfo routes to IPC or proxy.
func (h *Handlers) handleInfo(w http.ResponseWriter, r *http.Request) {
	if h.Config.ExtractorIPCEnabled {
		if !h.requireIPCReady(w) {
			return
		}
		h.handleInfoViaExtractorIPC(w, r)
		return
	}
	h.proxyWithRetry(w, r, "/info", http.MethodGet, "", false)
}

func (h *Handlers) handleInfoViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("url"))
	if url == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing url query parameter"})
		return
	}
	proxy := strings.TrimSpace(r.URL.Query().Get("proxy"))
	impersonate := strings.TrimSpace(r.URL.Query().Get("impersonate"))
	preferred := extractWorkerID(r.URL.Query().Get("key"), h.Config.WorkerCount)
	result, workerID, rpcErr, err := h.callExtractorMapWithFailover(preferred, false, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.ExtractInfo(wid, url, proxy, impersonate)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// handleDownload routes to IPC or proxy.
func (h *Handlers) handleDownload(w http.ResponseWriter, r *http.Request) {
	if !h.checkRateLimit(w, r, h.RLDownload, "download") {
		return
	}
	if h.Config.ExtractorIPCEnabled {
		if !h.requireIPCReady(w) {
			return
		}
		h.handleDownloadViaExtractorIPC(w, r)
		return
	}
	h.proxyWithRetry(w, r, "/download", http.MethodGet, extractWorkerID(r.URL.Query().Get("key"), h.Config.WorkerCount), true)
}

// handleDownloadViaExtractorIPC handles download via IPC with delivery planning.
func (h *Handlers) handleDownloadViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimSpace(r.URL.Query().Get("key"))
	if key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing key query parameter"})
		return
	}
	download := utilsParseBool(r.URL.Query().Get("download"), true)
	preferred := extractWorkerID(key, h.Config.WorkerCount)
	planRaw, workerID, rpcErr, err := h.callExtractorMapWithFailover(preferred, false, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.DownloadPrepare(wid, key, download)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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
	plan := delivery.ParseDeliveryPlan(planRaw)
	refreshFn := func() (delivery.DeliveryPlan, error) {
		np, rpcErr, err := h.Extractor.DownloadRefresh(workerID, key, download)
		if err != nil {
			return delivery.DeliveryPlan{}, err
		}
		if rpcErr != nil {
			return delivery.DeliveryPlan{}, fmt.Errorf("extractor rpc error (%s): %s", rpcErr.Code, rpcErr.Message)
		}
		return delivery.ParseDeliveryPlan(np), nil
	}

	if plan.NeedsFFmpeg {
		if plan.UseWorkerMP3 {
			h.Delivery.StreamWorkerMP3(w, r, workerID, plan, key)
			return
		}
		h.Delivery.StreamFFmpeg(w, r, workerID, plan, refreshFn)
		return
	}
	if plan.DeliveryMode == "single_progressive" && plan.SessionType == "video" {
		h.Delivery.StreamChunked(w, r, workerID, plan, refreshFn, delivery.VideoChunkSize)
		return
	}
	h.Delivery.StreamDirect(w, r, workerID, plan, refreshFn)
}

// handleStream routes stream endpoints to IPC or proxy.
func (h *Handlers) handleStream(w http.ResponseWriter, r *http.Request) {
	if h.Config.ExtractorIPCEnabled {
		if !h.requireIPCReady(w) {
			return
		}
		h.handleStreamViaExtractorIPC(w, r)
		return
	}
	h.proxyWithRotation(w, r, r.URL.Path)
}

func (h *Handlers) handleStreamViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("url"))
	if url == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing url query parameter"})
		return
	}
	proxy := strings.TrimSpace(r.URL.Query().Get("proxy"))
	impersonate := strings.TrimSpace(r.URL.Query().Get("impersonate"))
	download := utilsParseBool(r.URL.Query().Get("download"), false)
	// m4a special case
	if r.URL.Path == "/stream/m4a" {
		resolved, workerID, rpcErr, err := h.callExtractorMapWithFailover("", true, func(wid string) (map[string]any, *IPCError, error) {
			return h.Extractor.ResolveFormats(wid, url, "bestaudio[ext=m4a]/bestaudio", proxy, impersonate)
		})
		if workerID == "" {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
			return
		}
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
		respHeaders := map[string]string{"X-Accel-Buffering": "no"}
		if download {
			respHeaders = map[string]string{
				"Content-Disposition": "attachment; filename=\"audio.m4a\"",
				"X-Accel-Buffering":   "no",
			}
		}
		plan := delivery.DeliveryPlan{
			DirectURL:       directURL,
			RequestHeaders:  reqHeaders,
			ResponseHeaders: respHeaders,
			MediaType:       "audio/mp4",
			CanRefresh:      false,
		}
		h.Delivery.StreamDirect(w, r, workerID, plan, nil)
		return
	}

	fetchResult, workerID, rpcErr, err := h.callExtractorMapWithFailover("", true, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.Fetch(wid, url, proxy, impersonate)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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

	key := delivery.PickStreamDownloadKey(fetchResult, r.URL.Path, r.URL.Query().Get("quality"))
	if key == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Unable to resolve stream key from fetch result"})
		return
	}

	preferred := extractWorkerID(key, h.Config.WorkerCount)
	planRaw, workerID, rpcErr, err := h.callExtractorMapWithFailover(preferred, false, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.DownloadPrepare(wid, key, download)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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
	plan := delivery.ParseDeliveryPlan(planRaw)
	refreshFn := func() (delivery.DeliveryPlan, error) {
		np, rpcErr, err := h.Extractor.DownloadRefresh(workerID, key, download)
		if err != nil {
			return delivery.DeliveryPlan{}, err
		}
		if rpcErr != nil {
			return delivery.DeliveryPlan{}, fmt.Errorf("extractor rpc error (%s): %s", rpcErr.Code, rpcErr.Message)
		}
		return delivery.ParseDeliveryPlan(np), nil
	}

	if plan.NeedsFFmpeg {
		if plan.UseWorkerMP3 {
			h.Delivery.StreamWorkerMP3(w, r, workerID, plan, key)
			return
		}
		h.Delivery.StreamFFmpeg(w, r, workerID, plan, refreshFn)
		return
	}

	if plan.DeliveryMode == "single_progressive" {
		if r.URL.Path == "/stream/video-chunked" || r.URL.Path == "/stream/mp3-chunked" {
			chunkSize := int64(delivery.VideoChunkSize)
			if r.URL.Path == "/stream/mp3-chunked" {
				chunkSize = int64(delivery.AudioChunkSize)
			}
			h.Delivery.StreamChunked(w, r, workerID, plan, refreshFn, chunkSize)
			return
		}
		h.Delivery.StreamDirect(w, r, workerID, plan, refreshFn)
		return
	}

	if r.URL.Path == "/stream/video-chunked" || r.URL.Path == "/stream/mp3-chunked" {
		chunkSize := int64(delivery.VideoChunkSize)
		if r.URL.Path == "/stream/mp3-chunked" {
			chunkSize = int64(delivery.AudioChunkSize)
		}
		h.Delivery.StreamChunked(w, r, workerID, plan, refreshFn, chunkSize)
		return
	}
	h.Delivery.StreamDirect(w, r, workerID, plan, refreshFn)
}

// handleTikTok POST endpoint
func (h *Handlers) handleTikTok(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Method not allowed"})
		return
	}
	if h.Config.ExtractorIPCEnabled {
		if !h.requireIPCReady(w) {
			return
		}
		h.handleTikTokViaExtractorIPC(w, r)
		return
	}
	h.proxyWithRetry(w, r, "/tiktok", http.MethodPost, "", false)
}

type tiktokIPCRequest struct {
	URL         string `json:"url"`
	Proxy       string `json:"proxy"`
	Impersonate string `json:"impersonate"`
}

func (h *Handlers) handleTikTokViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
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
	result, workerID, rpcErr, err := h.callExtractorMapWithFailover("", true, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.TikTok(wid, payload.URL, payload.Proxy, payload.Impersonate)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// handleTikTokDownload GET endpoint
func (h *Handlers) handleTikTokDownload(w http.ResponseWriter, r *http.Request) {
	if h.Config.ExtractorIPCEnabled {
		if !h.requireIPCReady(w) {
			return
		}
		h.handleTikTokDownloadViaExtractorIPC(w, r)
		return
	}
	h.proxyWithRetry(w, r, "/tiktok/download", http.MethodGet, extractWorkerID(r.URL.Query().Get("key"), h.Config.WorkerCount), true)
}

func (h *Handlers) handleTikTokDownloadViaExtractorIPC(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimSpace(r.URL.Query().Get("key"))
	if key == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Missing key query parameter"})
		return
	}
	download := utilsParseBool(r.URL.Query().Get("download"), true)
	preferred := extractWorkerID(key, h.Config.WorkerCount)
	planRaw, workerID, rpcErr, err := h.callExtractorMapWithFailover(preferred, false, func(wid string) (map[string]any, *IPCError, error) {
		return h.Extractor.TikTokDownloadPrepare(wid, key, download)
	})
	if workerID == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
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
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	plan := delivery.ParseDeliveryPlan(planRaw)

	if plan.FallbackProxy {
		writeJSON(w, http.StatusBadGateway, map[string]string{
			"error":  "fallback_proxy_not_allowed",
			"detail": "EXTRACTOR_IPC_ENABLED=true requires full IPC mode",
		})
		return
	}
	if plan.ContentType == "slideshow" {
		refreshFn := func() (delivery.DeliveryPlan, error) {
			np, rpcErr, err := h.Extractor.TikTokDownloadRefresh(workerID, key, download)
			if err != nil {
				return delivery.DeliveryPlan{}, err
			}
			if rpcErr != nil {
				return delivery.DeliveryPlan{}, fmt.Errorf("extractor rpc error (%s): %s", rpcErr.Code, rpcErr.Message)
			}
			return delivery.ParseDeliveryPlan(np), nil
		}
		h.Delivery.StreamTikTokSlideshow(w, r, plan, refreshFn)
		return
	}

	// Direct URL stream with refresh
	if plan.DirectURL == "" {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return
	}

	h.Delivery.StreamDirect(w, r, workerID, plan, func() (delivery.DeliveryPlan, error) {
		np, rpcErr, err := h.Extractor.TikTokDownloadRefresh(workerID, key, download)
		if err != nil {
			return delivery.DeliveryPlan{}, err
		}
		if rpcErr != nil {
			return delivery.DeliveryPlan{}, fmt.Errorf("extractor rpc error (%s): %s", rpcErr.Code, rpcErr.Message)
		}
		return delivery.ParseDeliveryPlan(np), nil
	})
}

// handleTunnel
func (h *Handlers) handleTunnel(w http.ResponseWriter, r *http.Request) {
	workerID := extractWorkerID(r.URL.Query().Get("key"), h.Config.WorkerCount)
	if workerID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Invalid key"})
		return
	}
	worker := h.getPreferredOrHealthyWorker(workerID)
	if worker == nil {
		w.Header().Set("Retry-After", strconv.Itoa(h.Config.DegradedRetryAfter))
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "Worker not available"})
		return
	}
	result := h.streamFromWorker(w, r, worker, "/tunnel")
	if !result.success && !result.wroteDirect {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Stream failed"})
	}
}

// handle logNoHealthyWorkersSnapshot
func (h *Handlers) logNoHealthyWorkersSnapshot(tried map[string]bool) {
	workers := h.Registry.WorkersSnapshot()
	parts := make([]string, 0, len(workers))
	for _, w := range workers {
		reasons := make([]string, 0, 8)
		if tried != nil && tried[w.ID] {
			reasons = append(reasons, "tried")
		}
		if len(reasons) == 0 {
			reasons = append(reasons, "eligible")
		}
		parts = append(parts, "w:"+w.ID+"(active="+strconv.Itoa(w.ActiveRequests)+",reasons="+strings.Join(reasons, "|")+")")
	}
	log.Printf("[diag] no healthy workers snapshot: %s", strings.Join(parts, "; "))
}

// helpers
func utilsParseBool(raw string, fallback bool) bool {
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

func (h *Handlers) extractorWorkerCandidates(preferred string, requireHealthy bool) []string {
	if h.Extractor == nil {
		return nil
	}
	seen := map[string]bool{}
	candidates := make([]string, 0, h.Config.WorkerCount)
	add := func(workerID string) {
		if workerID == "" || seen[workerID] || !h.Extractor.HasWorker(workerID) {
			return
		}
		if requireHealthy && !h.isWorkerAcceptingRequests(h.Registry.GetWorker(workerID)) {
			return
		}
		seen[workerID] = true
		candidates = append(candidates, workerID)
	}

	add(preferred)
	for _, worker := range h.Registry.GetHealthyWorkers(nil) {
		add(worker.ID)
	}
	if !requireHealthy {
		for i := 1; i <= h.Config.WorkerCount; i++ {
			add("w" + strconv.Itoa(i))
		}
	}
	return candidates
}

func isRetryableExtractorRPC(rpcErr *IPCError) bool {
	if rpcErr == nil {
		return false
	}
	status := rpcErr.Status
	if status == 0 {
		return true
	}
	return status == http.StatusTooManyRequests || status >= http.StatusInternalServerError
}

func (h *Handlers) callExtractorMapWithFailover(
	preferred string,
	requireHealthy bool,
	call func(workerID string) (map[string]any, *IPCError, error),
) (map[string]any, string, *IPCError, error) {
	var zero map[string]any
	candidates := h.extractorWorkerCandidates(preferred, requireHealthy)
	if len(candidates) == 0 {
		return zero, "", nil, nil
	}

	var lastErr error
	var lastRPC *IPCError
	for i, workerID := range candidates {
		result, rpcErr, err := call(workerID)
		if err == nil && rpcErr == nil {
			return result, workerID, nil, nil
		}

		retryable := err != nil || isRetryableExtractorRPC(rpcErr)
		if retryable {
			if rpcErr != nil {
				h.scheduleWorkerRestart(workerID, rpcErr.Status == http.StatusTooManyRequests)
			} else {
				h.scheduleWorkerRestart(workerID, false)
			}
		}

		if err != nil {
			lastErr = err
		}
		if rpcErr != nil {
			lastRPC = rpcErr
		}

		if !retryable || i == len(candidates)-1 {
			return zero, workerID, lastRPC, lastErr
		}
	}
	return zero, "", lastRPC, lastErr
}
