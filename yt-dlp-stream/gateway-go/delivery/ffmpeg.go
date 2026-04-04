package delivery

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
)

// StreamFFmpeg pipes media through ffmpeg for transcoding or merging.
func (d *Delivery) StreamFFmpeg(
	w http.ResponseWriter,
	r *http.Request,
	workerID string,
	plan DeliveryPlan,
	onRefresh func() (DeliveryPlan, error),
) bool {
	if strings.TrimSpace(plan.DirectURL) == "" {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Invalid stream plan: missing direct_url"})
		return false
	}

	mediaClient := d.mediaHTTPClientForPlan(workerID, planToMap(plan))

	ffmpegArgs := []string{"-hide_banner", "-loglevel", "error"}
	var stdinSource io.Reader
	var extraFiles []*os.File
	feedWorkers := 0
	feedErrCh := make(chan error, 2)
	startFeeds := func() {}

	if plan.FFmpegAudioOnly {
		pipeR, pipeW := io.Pipe()
		stdinSource = pipeR
		ffmpegArgs = append(ffmpegArgs, "-i", "pipe:0")
		feedWorkers = 1
		startFeeds = func() {
			go func() {
				err := d.DownloadToWriter(
					r.Context(),
					pipeW,
					plan,
					selectPrimarySource,
					AudioChunkSize,
					onRefresh,
					mediaClient,
				)
				_ = pipeW.CloseWithError(err)
				feedErrCh <- err
			}()
		}
	} else if plan.MergeAV && strings.TrimSpace(plan.FFmpegAudioURL) != "" {
		videoR, videoW := io.Pipe()
		audioR, audioW, err := os.Pipe()
		if err != nil {
			WriteJSON(w, http.StatusInternalServerError, map[string]string{"error": "Failed to create ffmpeg audio pipe", "detail": err.Error()})
			return false
		}
		stdinSource = videoR
		extraFiles = []*os.File{audioR}
		ffmpegArgs = append(ffmpegArgs, "-i", "pipe:0", "-i", "pipe:3")
		feedWorkers = 2
		startFeeds = func() {
			go func() {
				err := d.DownloadToWriter(
					r.Context(),
					videoW,
					plan,
					selectPrimarySource,
					VideoChunkSize,
					onRefresh,
					mediaClient,
				)
				_ = videoW.CloseWithError(err)
				feedErrCh <- err
			}()
			go func() {
				audioPlan := DeliveryPlan{
					DirectURL:       plan.FFmpegAudioURL,
					RequestHeaders:  plan.FFmpegAudioHdrs,
					ResponseHeaders: plan.ResponseHeaders,
					MediaType:       plan.MediaType,
					CanRefresh:      plan.CanRefresh,
					Platform:        plan.Platform,
				}
				_ = d.DownloadToWriter(
					r.Context(),
					audioW,
					audioPlan,
					selectPrimarySource,
					AudioChunkSize,
					nil,
					mediaClient,
				)
				_ = audioW.Close()
				feedErrCh <- nil
			}()
		}
		defer audioR.Close()
	} else {
		if hdr := ffmpegHeaderString(plan.RequestHeaders); hdr != "" {
			ffmpegArgs = append(ffmpegArgs, "-headers", hdr)
		}
		ffmpegArgs = append(ffmpegArgs, "-i", plan.DirectURL)
		if plan.MergeAV && strings.TrimSpace(plan.FFmpegAudioURL) != "" {
			if hdr := ffmpegHeaderString(plan.FFmpegAudioHdrs); hdr != "" {
				ffmpegArgs = append(ffmpegArgs, "-headers", hdr)
			}
			ffmpegArgs = append(ffmpegArgs, "-i", plan.FFmpegAudioURL)
		}
	}

	// Output arguments
	if plan.FFmpegAudioOnly {
		ffmpegArgs = append(ffmpegArgs, "-vn", "-acodec", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1")
		if plan.MediaType == "" {
			plan.MediaType = "audio/mpeg"
		}
	} else if plan.MergeAV && strings.TrimSpace(plan.FFmpegAudioURL) != "" {
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
		if plan.MediaType == "" {
			plan.MediaType = "video/mp4"
		}
	} else {
		ffmpegArgs = append(ffmpegArgs, "-c", "copy", "-movflags", "frag_keyframe+empty_moov+faststart", "-f", "mp4", "pipe:1")
		if plan.MediaType == "" {
			plan.MediaType = "video/mp4"
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
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to create ffmpeg pipe", "detail": err.Error()})
		return false
	}
	var ffmpegErr bytes.Buffer
	cmd.Stderr = &ffmpegErr

	if err := cmd.Start(); err != nil {
		WriteJSON(w, http.StatusBadGateway, map[string]string{"error": "Failed to start ffmpeg", "detail": err.Error()})
		return false
	}
	startFeeds()

	for k, v := range plan.ResponseHeaders {
		w.Header().Set(k, v)
	}
	if w.Header().Get("Content-Type") == "" && plan.MediaType != "" {
		w.Header().Set("Content-Type", plan.MediaType)
	}
	w.WriteHeader(http.StatusOK)
	_, copyErr := copyBuffer(w, stdout)
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

// DownloadToFileSimple downloads a file directly (no Range requests) for simple cases like slideshow images.
func DownloadToFileSimple(ctx context.Context, client *http.Client, srcURL string, headers map[string]string, dstPath string) error {
	if client == nil {
		return fmt.Errorf("nil HTTP client")
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
		_, _ = io.Copy(io.Discard, resp.Body)
		return fmt.Errorf("upstream %d", resp.StatusCode)
	}
	f, err := os.Create(dstPath)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = copyBuffer(f, resp.Body)
	return err
}
