package delivery

import (
	"io"
	"log"
	"net/http"
	"strconv"
)

// StreamDirect passes through the upstream media URL to the client.
// Preserves Range/If-Range headers, performs one refresh on 403.
// For TikTok, the 403 response body is checked for permanent errors
// (geo-block, captcha) before attempting a refresh.
func (d *Delivery) StreamDirect(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan DeliveryPlan,
	onRefresh func() (DeliveryPlan, error),
) bool {
	if plan.DirectURL == "" {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return false
	}

	maxAttempts := 1
	if plan.CanRefresh && onRefresh != nil {
		maxAttempts = 2
	}

	currentURL := plan.DirectURL
	currentPlan := map[string]any{
		"direct_url":       plan.DirectURL,
		"request_headers":  plan.RequestHeaders,
		"response_headers": plan.ResponseHeaders,
		"media_type":       plan.MediaType,
		"can_refresh":      plan.CanRefresh,
		"platform":         plan.Platform,
		"ffmpeg_audio_url": plan.FFmpegAudioURL,
		"photo_urls":       plan.PhotoURLs,
	}
	currentReqHeaders := plan.RequestHeaders
	currentRespHeaders := plan.ResponseHeaders
	currentMediaType := plan.MediaType

	for attempt := 0; attempt < maxAttempts; attempt++ {
		mediaClient := d.mediaHTTPClientForPlan(workerID, currentPlan)
		req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, currentURL, nil)
		if err != nil {
			WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to create upstream request", "detail": err.Error()})
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
			WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Upstream request failed", "detail": err.Error()})
			return false
		}

		if resp.StatusCode == http.StatusForbidden && attempt == 0 && plan.CanRefresh && onRefresh != nil {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
			_ = resp.Body.Close()
			// For TikTok content, check if this is a permanent error that won't be fixed by refresh.
			if plan.Platform == "tiktok" || plan.Platform == "douyin" {
				if !ShouldRefreshTikTokForbidden(body) {
					CopyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true})
					w.WriteHeader(resp.StatusCode)
					if len(body) > 0 {
						_, _ = w.Write(body)
					}
					return true
				}
			}

			refreshedPlan, err := onRefresh()
			if err != nil {
				break
			}
			if refreshedPlan.DirectURL != "" {
				currentURL = refreshedPlan.DirectURL
			}
			currentPlan["direct_url"] = refreshedPlan.DirectURL
			currentPlan["request_headers"] = refreshedPlan.RequestHeaders
			currentPlan["response_headers"] = refreshedPlan.ResponseHeaders
			currentPlan["media_type"] = refreshedPlan.MediaType
			currentPlan["platform"] = refreshedPlan.Platform
			currentPlan["ffmpeg_audio_url"] = refreshedPlan.FFmpegAudioURL
			currentPlan["photo_urls"] = refreshedPlan.PhotoURLs
			currentReqHeaders = refreshedPlan.RequestHeaders
			currentRespHeaders = refreshedPlan.ResponseHeaders
			if refreshedPlan.MediaType != "" {
				currentMediaType = refreshedPlan.MediaType
			}
			continue
		}

		if resp.StatusCode >= 400 {
			CopyHeader(w.Header(), resp.Header, map[string]bool{"transfer-encoding": true})
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

	w.Header().Set("Retry-After", strconv.Itoa(d.Config.DegradedRetryAfter))
	WriteJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":  "Service Unavailable",
		"detail": "Download failed",
	})
	return false
}
