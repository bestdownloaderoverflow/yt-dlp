package handlers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	neturl "net/url"
	"strconv"
	"strings"
	"time"

	"gateway-go/delivery"
	"gateway-go/metrics"
)

// isIPCNetworkError reports whether an IPC call failure is a network-level
// issue (timeout, refused, EOF) worth retrying on a different worker.
// Semantic errors (rpcError) are not passed here and should never be retried.
func isIPCNetworkError(err error) bool {
	if err == nil {
		return false
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return true
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return true
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "connection refused") ||
		strings.Contains(msg, "connection reset") ||
		strings.Contains(msg, "broken pipe") ||
		strings.Contains(msg, "i/o timeout") ||
		strings.Contains(msg, "no such host") ||
		strings.Contains(msg, "unexpected eof")
}

func ipcErrorType(err error) string {
	if err == nil {
		return "unknown"
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return "timeout"
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return "eof"
	}
	msg := strings.ToLower(err.Error())
	switch {
	case strings.Contains(msg, "connection refused"):
		return "conn_refused"
	case strings.Contains(msg, "connection reset"):
		return "conn_reset"
	case strings.Contains(msg, "broken pipe"):
		return "broken_pipe"
	case strings.Contains(msg, "i/o timeout"):
		return "timeout"
	case strings.Contains(msg, "no such host"):
		return "dns"
	case strings.Contains(msg, "unexpected eof"):
		return "eof"
	default:
		return "network"
	}
}

// isRetryableTikTokRPCError marks transient TikTok semantic failures that are
// worth retrying on another worker/IP (same URL, different VPN exit).
func isRetryableTikTokRPCError(rpcErr *IPCError) bool {
	if rpcErr == nil {
		return false
	}
	msg := strings.ToLower(strings.TrimSpace(rpcErr.Message))
	code := strings.ToLower(strings.TrimSpace(rpcErr.Code))
	switch {
	case strings.Contains(msg, "unexpected response from webpage request"):
		return true
	case strings.Contains(msg, "verify you are human"),
		strings.Contains(msg, "captcha"),
		strings.Contains(msg, "challenge"):
		return true
	case strings.Contains(msg, "try again later"),
		strings.Contains(msg, "temporarily unavailable"):
		return true
	}
	return rpcErr.Status >= 500 && (code == "internal_error" || code == "download_error")
}

func isYouTubePlatform(platform string) bool {
	switch strings.ToLower(strings.TrimSpace(platform)) {
	case "youtube", "youtube:tab", "youtube:shorts":
		return true
	default:
		return false
	}
}

func (h *Handlers) maybePreferDirectYouTube(
	ctx context.Context,
	workerID string,
	plan delivery.DeliveryPlan,
) delivery.DeliveryPlan {
	if !isYouTubePlatform(plan.Platform) {
		return plan
	}

	plan.BypassProxy = true
	if err := h.Delivery.ProbeDirectAccess(ctx, plan); err != nil {
		log.Printf("[%s] youtube direct preflight failed (%v); falling back to worker proxy", workerID, err)
		plan.BypassProxy = false
	}
	return plan
}

func (h *Handlers) recordExtractFailure(endpoint string, failureType string) {
	metrics.IncExtractFailure(endpoint, failureType)
	reasonCode := normalizeReasonCode(failureType)
	metrics.IncExtractFailureReason(endpoint, failureType, reasonCode)
}

func (h *Handlers) recordFailover(path string, reason string) {
	metrics.IncFailover(path, reason)
}

func (h *Handlers) recordExtractFailureWithReason(endpoint string, failureType string, reasonCode string) {
	metrics.IncExtractFailure(endpoint, failureType)
	metrics.IncExtractFailureReason(endpoint, failureType, normalizeReasonCode(reasonCode))
}

func normalizeReasonCode(reason string) string {
	reason = strings.TrimSpace(strings.ToLower(reason))
	if reason == "" {
		return "unknown"
	}
	const maxLen = 48
	buf := make([]byte, 0, len(reason))
	for i := 0; i < len(reason); i++ {
		ch := reason[i]
		if (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') {
			buf = append(buf, ch)
			continue
		}
		buf = append(buf, '_')
	}
	normalized := strings.Trim(strings.Join(strings.FieldsFunc(string(buf), func(r rune) bool { return r == '_' }), "_"), "_")
	if normalized == "" {
		normalized = "unknown"
	}
	if len(normalized) > maxLen {
		normalized = normalized[:maxLen]
	}
	return normalized
}

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
			"degraded_remaining":        positiveSeconds(worker.DegradedUntil.Sub(now)),
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

// handleMetrics exposes Prometheus metrics.
func (h *Handlers) handleMetrics(w http.ResponseWriter, r *http.Request) {
	metrics.ServeHTTP(w, r)
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
	isYouTube := isYouTubeURL(url)
	forceIPv6 := h.Config.YouTubeFetchIPv6Only && isYouTube

	workerID := ""
	if forceIPv6 && len(h.Config.YouTubeFetchWorkers) > 0 {
		eligible := make([]string, 0, len(h.Config.YouTubeFetchWorkers))
		for _, configuredWorker := range h.Config.YouTubeFetchWorkers {
			if !h.Extractor.HasWorker(configuredWorker) {
				continue
			}
			candidate := h.Registry.GetWorker(configuredWorker)
			if h.isWorkerAcceptingRequests(candidate) {
				eligible = append(eligible, configuredWorker)
			}
		}
		if len(eligible) == 0 {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{
				"error":  "YouTube IPv6 worker unavailable",
				"detail": "No healthy worker available in configured YouTube IPv6 pool",
			})
			return
		}
		workerID = h.pickLeastLoadedExtractorWorker(eligible)
	} else {
		workerID = h.selectExtractorWorker(preferred, false)
	}
	if workerID == "" {
		h.recordExtractFailure("fetch", "no_worker")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
	result, rpcErr, err := h.Extractor.Fetch(workerID, url, proxy, impersonate, forceIPv6)
	if err != nil {
		log.Printf("[extractor:%s] fetch ipc error: %v", workerID, err)
		h.recordExtractFailureWithReason("fetch", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("fetch", "rpc_error", rpcErr.Code)
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
	workerID := h.selectExtractorWorker(preferred, true)
	if workerID == "" {
		h.recordExtractFailure("info", "no_worker")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
	result, rpcErr, err := h.Extractor.ExtractInfo(workerID, url, proxy, impersonate)
	if err != nil {
		log.Printf("[extractor:%s] ipc error: %v", workerID, err)
		h.recordExtractFailureWithReason("info", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("info", "rpc_error", rpcErr.Code)
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
	workerID := h.selectExtractorWorker(preferred, true)
	if workerID == "" {
		h.recordExtractFailure("download_prepare", "no_worker")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
	planRaw, rpcErr, err := h.Extractor.DownloadPrepare(workerID, key, download)
	if err != nil {
		log.Printf("[extractor:%s] download prepare ipc error: %v", workerID, err)
		h.recordExtractFailureWithReason("download_prepare", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("download_prepare", "rpc_error", rpcErr.Code)
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	plan := delivery.ParseDeliveryPlan(planRaw)
	plan = h.maybePreferDirectYouTube(r.Context(), workerID, plan)
	refreshFn := func() (delivery.DeliveryPlan, error) {
		np, rpcErr, err := h.Extractor.DownloadRefresh(workerID, key, download)
		if err != nil {
			return delivery.DeliveryPlan{}, err
		}
		if rpcErr != nil {
			return delivery.DeliveryPlan{}, fmt.Errorf("extractor rpc error (%s): %s", rpcErr.Code, rpcErr.Message)
		}
		refreshedPlan := delivery.ParseDeliveryPlan(np)
		refreshedPlan.BypassProxy = plan.BypassProxy
		return refreshedPlan, nil
	}

	if plan.NeedsFFmpeg {
		if plan.UseWorkerMP3 {
			if h.streamWorkerMP3WithFailover(w, r, workerID, plan, key) {
				return
			}
			log.Printf("[mp3] worker-stream unavailable for key %s; falling back to gateway ffmpeg", key)
			h.Delivery.StreamFFmpeg(w, r, workerID, plan, refreshFn)
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
		workerID := h.selectExtractorWorker("", true)
		if workerID == "" {
			h.recordExtractFailure("stream_m4a_resolve", "no_worker")
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
			return
		}
		resolved, rpcErr, err := h.Extractor.ResolveFormats(workerID, url, "bestaudio[ext=m4a]/bestaudio", proxy, impersonate)
		if err != nil {
			log.Printf("[extractor:%s] stream m4a resolve ipc error: %v", workerID, err)
			h.recordExtractFailureWithReason("stream_m4a_resolve", "ipc_error", ipcErrorType(err))
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
			return
		}
		if rpcErr != nil {
			h.recordExtractFailureWithReason("stream_m4a_resolve", "rpc_error", rpcErr.Code)
			status := rpcErr.Status
			if status < 400 || status > 599 {
				status = http.StatusBadRequest
			}
			writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
			return
		}
		formatsAny, _ := resolved["formats"].([]any)
		if len(formatsAny) == 0 {
			h.recordExtractFailure("stream_m4a_resolve", "empty_formats")
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "No resolved audio formats"})
			return
		}
		fmt0, _ := formatsAny[0].(map[string]any)
		directURL, _ := fmt0["url"].(string)
		if strings.TrimSpace(directURL) == "" {
			h.recordExtractFailure("stream_m4a_resolve", "invalid_url")
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
			Platform:        "youtube",
		}
		plan = h.maybePreferDirectYouTube(r.Context(), workerID, plan)
		h.Delivery.StreamDirect(w, r, workerID, plan, nil)
		return
	}

	workerID := h.selectExtractorWorker("", true)
	if workerID == "" {
		h.recordExtractFailure("stream_fetch", "no_worker")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
	fetchResult, rpcErr, err := h.Extractor.Fetch(workerID, url, proxy, impersonate, false)
	if err != nil {
		log.Printf("[extractor:%s] stream fetch ipc error: %v", workerID, err)
		h.recordExtractFailureWithReason("stream_fetch", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("stream_fetch", "rpc_error", rpcErr.Code)
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}

	key := delivery.PickStreamDownloadKey(fetchResult, r.URL.Path, r.URL.Query().Get("quality"))
	if key == "" {
		h.recordExtractFailure("stream_fetch", "missing_key")
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Unable to resolve stream key from fetch result"})
		return
	}

	preferred := extractWorkerID(key, h.Config.WorkerCount)
	workerID = h.selectExtractorWorker(preferred, false)
	if workerID == "" {
		h.recordExtractFailure("stream_download_prepare", "no_worker")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
		return
	}
	planRaw, rpcErr, err := h.Extractor.DownloadPrepare(workerID, key, download)
	if err != nil {
		log.Printf("[extractor:%s] stream download prepare ipc error: %v", workerID, err)
		h.recordExtractFailureWithReason("stream_download_prepare", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("stream_download_prepare", "rpc_error", rpcErr.Code)
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	plan := delivery.ParseDeliveryPlan(planRaw)
	plan = h.maybePreferDirectYouTube(r.Context(), workerID, plan)
	refreshFn := func() (delivery.DeliveryPlan, error) {
		np, rpcErr, err := h.Extractor.DownloadRefresh(workerID, key, download)
		if err != nil {
			return delivery.DeliveryPlan{}, err
		}
		if rpcErr != nil {
			return delivery.DeliveryPlan{}, fmt.Errorf("extractor rpc error (%s): %s", rpcErr.Code, rpcErr.Message)
		}
		refreshedPlan := delivery.ParseDeliveryPlan(np)
		refreshedPlan.BypassProxy = plan.BypassProxy
		return refreshedPlan, nil
	}

	if plan.NeedsFFmpeg {
		if plan.UseWorkerMP3 {
			if h.streamWorkerMP3WithFailover(w, r, workerID, plan, key) {
				return
			}
			log.Printf("[mp3] worker-stream unavailable for key %s; falling back to gateway ffmpeg", key)
			h.Delivery.StreamFFmpeg(w, r, workerID, plan, refreshFn)
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

func isYouTubeURL(rawURL string) bool {
	parsed, err := neturl.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		lower := strings.ToLower(rawURL)
		return strings.Contains(lower, "youtube.com") || strings.Contains(lower, "youtu.be")
	}
	host := strings.ToLower(parsed.Hostname())
	return host == "youtube.com" ||
		host == "www.youtube.com" ||
		host == "m.youtube.com" ||
		host == "music.youtube.com" ||
		host == "youtu.be"
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
	const maxAttempts = 3
	tried := make(map[string]bool, maxAttempts)
	workerID := h.selectExtractorWorker("", true)
	var (
		result map[string]any
		rpcErr *IPCError
		err    error
	)
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if workerID == "" {
			h.recordExtractFailure("tiktok", "no_worker")
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
			return
		}
		result, rpcErr, err = h.Extractor.TikTok(workerID, payload.URL, payload.Proxy, payload.Impersonate)
		if err != nil {
			if !isIPCNetworkError(err) {
				break
			}
			log.Printf("[extractor:%s] tiktok ipc error (attempt %d/%d): %v", workerID, attempt+1, maxAttempts, err)
			metrics.IncIPCError(workerID, "tiktok", ipcErrorType(err))
			tried[workerID] = true
			h.recordFailover("/tiktok", "ipc_network")
			workerID = h.selectExtractorWorkerAvoiding("", true, tried)
			continue
		}
		if rpcErr != nil && isRetryableTikTokRPCError(rpcErr) {
			log.Printf(
				"[extractor:%s] tiktok rpc retryable error (attempt %d/%d): %s",
				workerID, attempt+1, maxAttempts, rpcErr.Message,
			)
			tried[workerID] = true
			h.recordFailover("/tiktok", "rpc_retryable")
			workerID = h.selectExtractorWorkerAvoiding("", true, tried)
			rpcErr = nil
			continue
		}
		if err == nil {
			break
		}
	}
	if err != nil {
		h.recordExtractFailureWithReason("tiktok", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("tiktok", "rpc_error", rpcErr.Code)
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

	const maxAttempts = 3
	tried := make(map[string]bool, maxAttempts)
	workerID := h.selectExtractorWorker(preferred, true)
	var (
		planRaw map[string]any
		rpcErr  *IPCError
		err     error
	)
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if workerID == "" {
			h.recordExtractFailure("tiktok_download_prepare", "no_worker")
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "No extractor workers available"})
			return
		}
		planRaw, rpcErr, err = h.Extractor.TikTokDownloadPrepare(workerID, key, download)
		if err != nil {
			if !isIPCNetworkError(err) {
				break
			}
			log.Printf("[extractor:%s] tiktok download prepare ipc error (attempt %d/%d): %v", workerID, attempt+1, maxAttempts, err)
			metrics.IncIPCError(workerID, "tiktok_download_prepare", ipcErrorType(err))
			tried[workerID] = true
			// Session state lives in shared Redis, so any healthy worker can take over.
			h.recordFailover("/tiktok/download", "ipc_network")
			workerID = h.selectExtractorWorkerAvoiding("", true, tried)
			continue
		}
		if rpcErr != nil && isRetryableTikTokRPCError(rpcErr) {
			log.Printf(
				"[extractor:%s] tiktok download prepare retryable error (attempt %d/%d): %s",
				workerID, attempt+1, maxAttempts, rpcErr.Message,
			)
			tried[workerID] = true
			h.recordFailover("/tiktok/download", "rpc_retryable")
			workerID = h.selectExtractorWorkerAvoiding("", true, tried)
			rpcErr = nil
			continue
		}
		if err == nil {
			break
		}
	}
	if err != nil {
		h.recordExtractFailureWithReason("tiktok_download_prepare", "ipc_error", ipcErrorType(err))
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Extractor IPC failure", "detail": err.Error()})
		return
	}
	if rpcErr != nil {
		h.recordExtractFailureWithReason("tiktok_download_prepare", "rpc_error", rpcErr.Code)
		status := rpcErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, map[string]string{"error": rpcErr.Code, "detail": rpcErr.Message})
		return
	}
	plan := delivery.ParseDeliveryPlan(planRaw)

	if plan.FallbackProxy {
		h.recordExtractFailure("tiktok_download_prepare", "fallback_proxy_not_allowed")
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
		h.recordExtractFailure("tiktok_download_prepare", "invalid_plan")
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
	now := time.Now()
	parts := make([]string, 0, len(workers))
	for _, w := range workers {
		reasons := make([]string, 0, 8)
		if tried != nil && tried[w.ID] {
			reasons = append(reasons, "tried")
		}
		if !w.Healthy {
			reasons = append(reasons, "unhealthy")
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
		if !w.DegradedUntil.IsZero() && now.Before(w.DegradedUntil) {
			reasons = append(reasons, "degraded("+strconv.Itoa(positiveSeconds(w.DegradedUntil.Sub(now)))+"s)")
		}
		if !w.QuarantineUntil.IsZero() && now.Before(w.QuarantineUntil) {
			reasons = append(reasons, "quarantine("+strconv.Itoa(positiveSeconds(w.QuarantineUntil.Sub(now)))+"s)")
		}
		if !w.LastRateLimit.IsZero() && now.Sub(w.LastRateLimit) <= time.Duration(h.Config.RateLimitCooldownSeconds)*time.Second {
			reasons = append(reasons, "rate_limited")
		}
		if len(reasons) == 0 {
			reasons = append(reasons, "eligible")
		}
		parts = append(parts, "w:"+w.ID+"(active="+strconv.Itoa(w.ActiveRequests)+","+strings.Join(reasons, "|")+")")
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

func (h *Handlers) streamWorkerMP3WithFailover(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan delivery.DeliveryPlan,
	key string,
) bool {
	if workerID == "" {
		return false
	}
	err := h.Delivery.StreamWorkerMP3(w, r, workerID, plan, key)
	if err == nil {
		return true
	}
	if delivery.IsResponseCommittedError(err) {
		return true
	}
	log.Printf("[mp3:%s] worker stream failed: %v", workerID, err)
	h.recordFailover("/stream/mp3", "worker_to_gateway")
	return false
}
