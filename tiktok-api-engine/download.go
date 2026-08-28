package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const desktopUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
	"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

func contentTypeFor(kind string) string {
	switch kind {
	case "mp3":
		return "audio/mpeg"
	case "photo":
		return "image/jpeg"
	default:
		return "video/mp4"
	}
}

func contentDisposition(filename string, asAttachment bool) string {
	mode := "inline"
	if asAttachment {
		mode = "attachment"
	}
	return fmt.Sprintf("%s; filename=%q; filename*=UTF-8''%s",
		mode, filename, url.PathEscape(filename))
}

// cdnClient is separate from the API client: downloads may be routed
// independently of extraction, and they carry no device fingerprint.
func (s *Server) cdnRequest(ctx context.Context, target string, rangeHeader string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", desktopUserAgent)
	// The post's own page, matching what a real player sends.
	req.Header.Set("Referer", "https://www.tiktok.com/")
	req.Header.Set("Accept", "*/*")
	req.Header.Set("Accept-Encoding", "identity")
	if rangeHeader != "" {
		req.Header.Set("Range", rangeHeader)
	}
	return s.cdn.Do(req)
}

func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request) {
	key := r.URL.Query().Get("key")
	if key == "" {
		writeError(w, http.StatusBadRequest, "Download session expired or invalid")
		return
	}
	session, ok := s.sessions.Get(key)
	if !ok {
		writeError(w, http.StatusNotFound, "Download session expired or invalid")
		return
	}
	asAttachment := r.URL.Query().Get("download") != "false" &&
		r.URL.Query().Get("download") != "0"

	switch session.Type {
	case "slideshow", "slideshow_render":
		s.serveSlideshow(w, r, session, asAttachment)
	case "mp3":
		s.serveMP3(w, r, session, asAttachment)
	default:
		s.serveDirect(w, r, session, asAttachment)
	}
}

// serveDirect proxies video and photo bytes straight from the CDN, passing the
// client's Range through so seeking keeps working.
func (s *Server) serveDirect(w http.ResponseWriter, r *http.Request, session SessionData, asAttachment bool) {
	if session.DirectURL == "" {
		writeError(w, http.StatusBadRequest, "Session does not contain direct_url")
		return
	}
	rangeHeader := r.Header.Get("Range")
	if rangeHeader == "" && session.Type == "video" {
		rangeHeader = "bytes=0-"
	}
	resp, err := s.cdnRequest(r.Context(), session.DirectURL, rangeHeader)
	if err != nil {
		writeError(w, http.StatusBadGateway, "CDN streaming error: "+err.Error())
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
		writeError(w, http.StatusBadGateway,
			fmt.Sprintf("CDN streaming error: HTTP %d", resp.StatusCode))
		return
	}

	filename := BuildFilename(session)
	h := w.Header()
	if ct := resp.Header.Get("Content-Type"); ct != "" {
		h.Set("Content-Type", ct)
	} else {
		h.Set("Content-Type", contentTypeFor(session.Type))
	}
	h.Set("Content-Disposition", contentDisposition(filename, asAttachment))
	h.Set("Accept-Ranges", "bytes")
	if v := resp.Header.Get("Content-Length"); v != "" {
		h.Set("Content-Length", v)
	}
	if v := resp.Header.Get("Content-Range"); v != "" {
		h.Set("Content-Range", v)
	}
	w.WriteHeader(resp.StatusCode)
	if r.Method == http.MethodHead {
		return
	}
	io.Copy(w, resp.Body)
}

// serveMP3 pipes the CDN body through ffmpeg. The upstream response is checked
// before any header is written, so a dead URL is a 502 rather than a truncated
// audio file.
func (s *Server) serveMP3(w http.ResponseWriter, r *http.Request, session SessionData, asAttachment bool) {
	if session.DirectURL == "" {
		writeError(w, http.StatusBadRequest, "Session does not contain direct_url")
		return
	}
	resp, err := s.cdnRequest(r.Context(), session.DirectURL, "")
	if err != nil {
		writeError(w, http.StatusBadGateway, "CDN streaming error: "+err.Error())
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
		writeError(w, http.StatusBadGateway,
			fmt.Sprintf("CDN streaming error: HTTP %d", resp.StatusCode))
		return
	}

	filename := BuildFilename(session)
	w.Header().Set("Content-Type", "audio/mpeg")
	w.Header().Set("Content-Disposition", contentDisposition(filename, asAttachment))
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}

	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	cmd := exec.CommandContext(ctx, "ffmpeg", "-hide_banner", "-loglevel", "error",
		"-i", "pipe:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1")
	cmd.Stdin = resp.Body
	cmd.Stdout = w
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		// Headers are already out; all that is left is to stop and log.
		log.Printf("[download] mp3 transcode failed for %s: %v", session.DirectURL, err)
	}
	s.metrics.Inc("tiktok_download_requests_total", "media_type", "mp3")
}

// serveSlideshow renders first and streams second.
//
// Doing it the other way -- rendering inside the response body -- means a
// failed ffmpeg run reaches the client as a 200 with an empty file, because the
// headers have already gone out. Nothing is lost by rendering first: the muxed
// file has to be complete before its first byte can be sent anyway.
func (s *Server) serveSlideshow(w http.ResponseWriter, r *http.Request, session SessionData, asAttachment bool) {
	if len(session.PhotoURLs) == 0 {
		writeError(w, http.StatusBadRequest, "No photos in slideshow session")
		return
	}
	if len(session.PhotoURLs) > s.cfg.SlideshowMaxImages {
		writeError(w, http.StatusBadRequest, "Slideshow has too many photos to render")
		return
	}

	dir, err := os.MkdirTemp("", "tiktok_slideshow_")
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Cannot allocate scratch space")
		return
	}
	defer os.RemoveAll(dir)

	out, err := s.renderSlideshow(r.Context(), dir, session)
	if err != nil {
		log.Printf("[slideshow] render failed: %v", err)
		s.metrics.Inc("tiktok_slideshow_render_total", "result", "failed")
		writeError(w, http.StatusBadGateway, "Slideshow render failed")
		return
	}
	info, err := os.Stat(out)
	if err != nil || info.Size() == 0 {
		s.metrics.Inc("tiktok_slideshow_render_total", "result", "failed")
		writeError(w, http.StatusBadGateway, "Slideshow render failed")
		return
	}
	s.metrics.Inc("tiktok_slideshow_render_total", "result", "success")

	filename := BuildFilename(session)
	w.Header().Set("Content-Type", "video/mp4")
	w.Header().Set("Content-Disposition", contentDisposition(filename, asAttachment))
	// The file is finished, so the length is known and the client gets a
	// real progress bar instead of an open-ended stream.
	w.Header().Set("Content-Length", strconv.FormatInt(info.Size(), 10))
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	f, err := os.Open(out)
	if err != nil {
		return
	}
	defer f.Close()
	io.Copy(w, f)
}

func (s *Server) renderSlideshow(ctx context.Context, dir string, session SessionData) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()

	var imagePaths []string
	for i, imgURL := range session.PhotoURLs {
		resp, err := s.cdnRequest(ctx, imgURL, "")
		if err != nil {
			return "", fmt.Errorf("photo %d: %w", i+1, err)
		}
		if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
			resp.Body.Close()
			// Writing an error page out as .jpg only fails later inside ffmpeg,
			// where the cause is no longer visible.
			return "", fmt.Errorf("photo %d: HTTP %d", i+1, resp.StatusCode)
		}
		path := filepath.Join(dir, fmt.Sprintf("img_%03d.jpg", i))
		f, err := os.Create(path)
		if err != nil {
			resp.Body.Close()
			return "", err
		}
		_, err = io.Copy(f, resp.Body)
		resp.Body.Close()
		f.Close()
		if err != nil {
			return "", fmt.Errorf("photo %d: %w", i+1, err)
		}
		imagePaths = append(imagePaths, path)
	}
	if len(imagePaths) == 0 {
		return "", fmt.Errorf("no photos could be fetched")
	}

	// Audio is optional: a silent slideshow beats no slideshow.
	audioPath := ""
	if session.AudioURL != "" {
		if resp, err := s.cdnRequest(ctx, session.AudioURL, ""); err == nil {
			if resp.StatusCode == http.StatusOK {
				p := filepath.Join(dir, "audio.mp3")
				if f, ferr := os.Create(p); ferr == nil {
					if _, cerr := io.Copy(f, resp.Body); cerr == nil {
						audioPath = p
					}
					f.Close()
				}
			}
			resp.Body.Close()
		}
	}

	perImage := s.cfg.SlideshowSecondsPerImage
	listPath := filepath.Join(dir, "images.txt")
	var list strings.Builder
	for _, p := range imagePaths {
		fmt.Fprintf(&list, "file '%s'\nduration %d\n", p, perImage)
	}
	// concat demuxer ignores the final entry's duration, so the last image is
	// repeated to give it its full share of screen time.
	fmt.Fprintf(&list, "file '%s'\n", imagePaths[len(imagePaths)-1])
	if err := os.WriteFile(listPath, []byte(list.String()), 0o600); err != nil {
		return "", err
	}

	outPath := filepath.Join(dir, "slideshow.mp4")
	args := []string{"-y", "-hide_banner", "-loglevel", "error",
		"-f", "concat", "-safe", "0", "-i", listPath}
	filter := "[0:v]scale=w=720:h=1280:force_original_aspect_ratio=decrease," +
		"pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=24[vout]"
	if audioPath != "" {
		total := len(imagePaths) * perImage
		args = append(args, "-stream_loop", "-1", "-i", audioPath)
		filter += fmt.Sprintf(";[1:a]atrim=0:%d,asetpts=PTS-STARTPTS[aout]", total)
		args = append(args, "-filter_complex", filter, "-map", "[vout]", "-map", "[aout]")
	} else {
		args = append(args, "-filter_complex", filter, "-map", "[vout]")
	}
	args = append(args, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
		"-pix_fmt", "yuv420p", outPath)

	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	stderr := &strings.Builder{}
	cmd.Stderr = stderr
	if err := cmd.Run(); err != nil {
		tail := stderr.String()
		if len(tail) > 500 {
			tail = tail[len(tail)-500:]
		}
		return "", fmt.Errorf("ffmpeg: %w: %s", err, tail)
	}
	return outPath, nil
}
