package delivery

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// StreamChunked performs HTTP Range requests in sequential chunks (10MB video / 8MB audio).
// Falls back to StreamDirect if total size is unknown.
func (d *Delivery) StreamChunked(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan DeliveryPlan,
	onRefresh func() (DeliveryPlan, error),
	chunkSize int64,
) {
	if strings.TrimSpace(plan.DirectURL) == "" {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return
	}
	if chunkSize <= 0 {
		chunkSize = VideoChunkSize
	}

	totalSize := parsePositiveInt64(plan.ResponseHeaders["Content-Length"])
	if totalSize <= 0 {
		// Without exact size, fall back to direct stream for compatibility.
		d.StreamDirect(w, r, workerID, plan, onRefresh)
		return
	}

	for k, v := range plan.ResponseHeaders {
		kl := strings.ToLower(k)
		if kl == "content-length" || kl == "content-range" {
			continue
		}
		w.Header().Set(k, v)
	}
	if w.Header().Get("Content-Type") == "" && plan.MediaType != "" {
		w.Header().Set("Content-Type", plan.MediaType)
	}
	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Content-Length", strconv.FormatInt(totalSize, 10))
	w.WriteHeader(http.StatusOK)

	currentURL := plan.DirectURL
	currentReqHeaders := plan.RequestHeaders
	currentPlan := map[string]any{
		"direct_url":       plan.DirectURL,
		"request_headers":  plan.RequestHeaders,
		"response_headers": plan.ResponseHeaders,
		"media_type":       plan.MediaType,
		"can_refresh":      plan.CanRefresh,
		"platform":         plan.Platform,
		"ffmpeg_audio_url": plan.FFmpegAudioURL,
		"photo_urls":       []string{plan.DirectURL},
	}
	read := int64(0)
	mediaClient := d.mediaHTTPClient(workerID)
	refreshAttempts := 0
	const maxRefreshAttempts = 10
	const maxRetriesPerChunk = 3

	for read < totalSize {
		end := read + chunkSize - 1
		if end >= totalSize {
			end = totalSize - 1
		}

		chunkComplete := false
		for chunkRetry := 0; chunkRetry < maxRetriesPerChunk && !chunkComplete; chunkRetry++ {
			req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, currentURL, nil)
			if err != nil {
				return
			}
			for k, v := range currentReqHeaders {
				req.Header.Set(k, v)
			}
			req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", read, end))

			resp, err := mediaClient.Do(req)
			if err != nil {
				if r.Context().Err() != nil || isClientAbortError(err) {
					return
				}
				if chunkRetry == maxRetriesPerChunk-1 {
					log.Printf("chunked upstream request failed at %d-%d: %v", read, end, err)
					return
				}
				time.Sleep(time.Duration(1<<chunkRetry) * time.Second)
				continue
			}

			if resp.StatusCode == http.StatusForbidden && plan.CanRefresh && onRefresh != nil && refreshAttempts < maxRefreshAttempts {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				refreshedPlan, refreshErr := onRefresh()
				if refreshErr != nil {
					log.Printf("chunked refresh failed at %d-%d: %v", read, end, refreshErr)
					return
				}
				if refreshedPlan.DirectURL != "" {
					currentURL = refreshedPlan.DirectURL
				}
				currentPlan["direct_url"] = refreshedPlan.DirectURL
				currentPlan["request_headers"] = refreshedPlan.RequestHeaders
				currentReqHeaders = refreshedPlan.RequestHeaders
				refreshAttempts++
				continue
			}

			if resp.StatusCode != http.StatusPartialContent && !(resp.StatusCode == http.StatusOK && read == 0) {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				if chunkRetry == maxRetriesPerChunk-1 {
					log.Printf("chunked upstream status %d at %d-%d", resp.StatusCode, read, end)
					return
				}
				time.Sleep(time.Duration(1<<chunkRetry) * time.Second)
				continue
			}

			n, copyErr := copyBuffer(w, resp.Body)
			_ = resp.Body.Close()
			if copyErr != nil {
				if isClientAbortError(copyErr) || r.Context().Err() != nil {
					return
				}
				log.Printf("chunked stream copy error at %d-%d: %v", read, end, copyErr)
				return
			}
			read += n

			// Some servers ignore range and stream whole body on first request.
			if resp.StatusCode == http.StatusOK {
				return
			}
			if n <= 0 {
				return
			}
			chunkComplete = true
		}
		if !chunkComplete {
			return
		}
	}
}

// DownloadToFile downloads a source from a plan to a local file using HTTP Range requests.
// Returns the updated plan after potential URL refreshes.
func (d *Delivery) DownloadToFile(
	ctx context.Context,
	dstPath string,
	plan DeliveryPlan,
	selector planSourceSelector,
	chunkSize int64,
	onRefresh func() (DeliveryPlan, error),
	client *http.Client,
) (DeliveryPlan, error) {
	if selector == nil {
		return plan, fmt.Errorf("missing source selector")
	}
	if client == nil {
		client = d.BaseCl
	}
	if chunkSize <= 0 {
		chunkSize = VideoChunkSize
	}

	currentPlan := map[string]any{
		"direct_url":       plan.DirectURL,
		"request_headers":  plan.RequestHeaders,
		"response_headers": plan.ResponseHeaders,
		"media_type":       plan.MediaType,
		"can_refresh":      plan.CanRefresh,
		"platform":         plan.Platform,
		"ffmpeg_audio_url": plan.FFmpegAudioURL,
		"photo_urls":       []string{plan.DirectURL},
	}
	currentURL, currentHeaders := selector(currentPlan)
	currentHeaders = sanitizeRequestHeaders(currentHeaders)
	if strings.TrimSpace(currentURL) == "" {
		return plan, fmt.Errorf("missing source URL in plan")
	}

	f, err := os.Create(dstPath)
	if err != nil {
		return plan, err
	}
	defer f.Close()

	var read int64
	var total int64
	refreshAttempts := 0
	const maxRefreshAttempts = 3

	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, currentURL, nil)
		if err != nil {
			return plan, err
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

		resp, err := client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return plan, ctx.Err()
			}
			return plan, err
		}

		if resp.StatusCode == http.StatusForbidden && plan.CanRefresh && onRefresh != nil && refreshAttempts < maxRefreshAttempts {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			refreshedPlan, refreshErr := onRefresh()
			if refreshErr != nil {
				return plan, refreshErr
			}
			currentPlan["direct_url"] = refreshedPlan.DirectURL
			currentPlan["request_headers"] = refreshedPlan.RequestHeaders
			currentPlan["photo_urls"] = []string{refreshedPlan.DirectURL}
			currentURL, currentHeaders = selector(currentPlan)
			currentHeaders = sanitizeRequestHeaders(currentHeaders)
			if strings.TrimSpace(currentURL) == "" {
				return plan, fmt.Errorf("missing refreshed source URL in plan")
			}
			refreshAttempts++
			continue
		}

		if resp.StatusCode == http.StatusRequestedRangeNotSatisfiable {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if total > 0 && read >= total {
				return plan, nil
			}
			return plan, fmt.Errorf("range not satisfiable at %d/%d", read, total)
		}

		if resp.StatusCode != http.StatusPartialContent && resp.StatusCode != http.StatusOK {
			bodySnippet, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
			_ = resp.Body.Close()
			return plan, fmt.Errorf("upstream status %d: %s", resp.StatusCode, strings.TrimSpace(string(bodySnippet)))
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
			if _, err := f.Seek(0, io.SeekStart); err != nil {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				return plan, err
			}
			if err := f.Truncate(0); err != nil {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				return plan, err
			}
			read = 0
			total = parsePositiveInt64(resp.Header.Get("Content-Length"))
		}

		n, copyErr := copyBuffer(f, resp.Body)
		_ = resp.Body.Close()
		if copyErr != nil {
			if ctx.Err() != nil {
				return plan, ctx.Err()
			}
			return plan, copyErr
		}
		read += n

		if resp.StatusCode == http.StatusOK {
			return plan, nil
		}
		if total > 0 && read >= total {
			return plan, nil
		}
		if n <= 0 {
			return plan, nil
		}
	}
}

// DownloadToWriter downloads a source from a plan into a writer using HTTP Range requests.
func (d *Delivery) DownloadToWriter(
	ctx context.Context,
	dst io.WriteCloser,
	plan DeliveryPlan,
	selector planSourceSelector,
	chunkSize int64,
	onRefresh func() (DeliveryPlan, error),
	client *http.Client,
) error {
	if dst == nil {
		return fmt.Errorf("missing destination writer")
	}
	if selector == nil {
		return fmt.Errorf("missing source selector")
	}
	if client == nil {
		client = d.BaseCl
	}
	if chunkSize <= 0 {
		chunkSize = VideoChunkSize
	}

	currentPlan := map[string]any{
		"direct_url":       plan.DirectURL,
		"request_headers":  plan.RequestHeaders,
		"response_headers": plan.ResponseHeaders,
		"media_type":       plan.MediaType,
		"can_refresh":      plan.CanRefresh,
		"platform":         plan.Platform,
		"ffmpeg_audio_url": plan.FFmpegAudioURL,
		"photo_urls":       []string{plan.DirectURL},
	}
	currentURL, currentHeaders := selector(currentPlan)
	currentHeaders = sanitizeRequestHeaders(currentHeaders)
	if strings.TrimSpace(currentURL) == "" {
		return fmt.Errorf("missing source URL in plan")
	}

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

		resp, err := client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}

		if resp.StatusCode == http.StatusForbidden && plan.CanRefresh && onRefresh != nil && refreshAttempts < maxRefreshAttempts {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			refreshedPlan, refreshErr := onRefresh()
			if refreshErr != nil {
				return refreshErr
			}
			currentPlan["direct_url"] = refreshedPlan.DirectURL
			currentPlan["request_headers"] = refreshedPlan.RequestHeaders
			currentPlan["photo_urls"] = []string{refreshedPlan.DirectURL}
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

		n, copyErr := copyBuffer(dst, resp.Body)
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
