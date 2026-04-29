package delivery

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// slideshowFFmpegTimeout bounds how long a single slideshow encode may run
// before it is killed. Keeps stuck/slow encodes from being reaped by the
// kernel OOM killer (which is what produced "signal: killed" in logs).
const slideshowFFmpegTimeout = 90 * time.Second

// StreamTikTokSlideshow renders and streams a TikTok photo slideshow as MP4.
func (d *Delivery) StreamTikTokSlideshow(
	w http.ResponseWriter,
	r *http.Request,
	plan DeliveryPlan,
	onRefresh func() (DeliveryPlan, error),
) {
	currentPlan := plan
	for attempt := 0; attempt < 2; attempt++ {
		outputPath, mediaType, responseHeaders, tempDir, statusCode, err := d.renderTikTokSlideshow(r.Context(), currentPlan)
		if err == nil {
			defer os.RemoveAll(tempDir)
			for k, v := range responseHeaders {
				w.Header().Set(k, v)
			}
			w.Header().Set("Content-Type", mediaType)

			f, openErr := os.Open(outputPath)
			if openErr != nil {
				WriteJSON(w, http.StatusInternalServerError, map[string]string{"error": "Failed to open slideshow output", "detail": openErr.Error()})
				return
			}
			defer f.Close()
			if fi, statErr := f.Stat(); statErr == nil && fi.Size() > 0 {
				w.Header().Set("Content-Length", strconv.FormatInt(fi.Size(), 10))
			}
			w.WriteHeader(http.StatusOK)
			_, copyErr := copyBuffer(w, f)
			if copyErr != nil && !isClientAbortError(copyErr) {
				log.Printf("slideshow stream copy error: %v", copyErr)
			}
			return
		}

		if tempDir != "" {
			_ = os.RemoveAll(tempDir)
		}

		// Retry once with a fresh extraction on failure.
		if attempt == 0 && onRefresh != nil {
			refreshedPlan, err := onRefresh()
			if err == nil {
				currentPlan = refreshedPlan
				continue
			}
		}

		if statusCode < 400 || statusCode > 599 {
			statusCode = http.StatusBadGateway
		}
		WriteJSON(w, statusCode, map[string]string{
			"error":  "Failed to render slideshow",
			"detail": err.Error(),
		})
		return
	}
	WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to render slideshow", "detail": "Unknown slideshow rendering failure"})
}

func (d *Delivery) renderTikTokSlideshow(ctx context.Context, plan DeliveryPlan) (string, string, map[string]string, string, int, error) {
	photoURLs := plan.PhotoURLs
	if len(photoURLs) == 0 {
		return "", "", nil, "", http.StatusBadRequest, fmt.Errorf("no photos available for slideshow")
	}
	audioURL := plan.AudioURL
	durationPerImage := plan.DurationPerImage
	if durationPerImage < 1 {
		durationPerImage = 4
	}
	responseHeaders := plan.ResponseHeaders
	mediaType := plan.MediaType
	if mediaType == "" {
		mediaType = "video/mp4"
	}

	tempDir, err := os.MkdirTemp("", "tiktok_slideshow_")
	if err != nil {
		return "", "", nil, "", http.StatusInternalServerError, fmt.Errorf("failed to create temp dir: %w", err)
	}

	client := d.BaseCl
	imagePaths := make([]string, 0, len(photoURLs))
	for i, u := range photoURLs {
		dst := filepath.Join(tempDir, fmt.Sprintf("image_%d.jpg", i))
		if err := downloadImage(ctx, client, u, dst); err != nil {
			return "", "", nil, tempDir, http.StatusBadGateway, fmt.Errorf("failed to download slideshow image: %w", err)
		}
		imagePaths = append(imagePaths, dst)
	}

	audioPath := ""
	if strings.TrimSpace(audioURL) != "" {
		audioPath = filepath.Join(tempDir, "audio.mp3")
		if err := downloadImage(ctx, client, audioURL, audioPath); err != nil {
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
			"-preset", "ultrafast",
			"-tune", "stillimage",
			"-crf", "28",
			"-b:v", "320k",
			"-maxrate", "360k",
			"-bufsize", "720k",
			"-threads", "1",
			"-max_muxing_queue_size", "1024",
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
			"-preset", "ultrafast",
			"-tune", "stillimage",
			"-crf", "28",
			"-b:v", "320k",
			"-maxrate", "360k",
			"-bufsize", "720k",
			"-threads", "1",
			"-max_muxing_queue_size", "1024",
			outputPath,
		)
	}

	ffCtx, cancelFF := context.WithTimeout(ctx, slideshowFFmpegTimeout)
	defer cancelFF()
	cmd := exec.CommandContext(ffCtx, "ffmpeg", ffmpegArgs...)
	if out, err := cmd.CombinedOutput(); err != nil {
		detail := strings.TrimSpace(string(out))
		if errors.Is(ffCtx.Err(), context.DeadlineExceeded) {
			if detail == "" {
				detail = fmt.Sprintf("timed out after %s", slideshowFFmpegTimeout)
			}
			log.Printf("slideshow ffmpeg timeout after %s, out=%s", slideshowFFmpegTimeout, detail)
			return "", "", nil, tempDir, http.StatusGatewayTimeout, fmt.Errorf("ffmpeg timed out after %s", slideshowFFmpegTimeout)
		}
		if detail == "" {
			detail = err.Error()
		}
		log.Printf("slideshow ffmpeg error: %v, out=%s", err, detail)
		return "", "", nil, tempDir, http.StatusBadGateway, fmt.Errorf("ffmpeg failed: %s", detail)
	}
	return outputPath, mediaType, responseHeaders, tempDir, http.StatusOK, nil
}

func downloadImage(ctx context.Context, client *http.Client, srcURL string, dstPath string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, srcURL, nil)
	if err != nil {
		return err
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
	_, err = copyBuffer(f, resp.Body)
	return err
}

// ShouldRefreshTikTokForbidden checks if a 403 response body is a transient error
// (worth refreshing) or a permanent error (geo-block, login, captcha).
func ShouldRefreshTikTokForbidden(body []byte) bool {
	text := strings.ToLower(string(body))
	if strings.TrimSpace(text) == "" {
		return true
	}
	permanentPatterns := []string{
		"geo_restricted",
		"geo restricted",
		"do not have permission",
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
