package main

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

var startTime = time.Now()

type Server struct {
	cfg      Config
	client   *Client
	sessions *Store
	metrics  *Metrics
	cdn      *http.Client
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(payload)
}

// writeError matches the 9111 error envelope exactly: {"detail": "..."}.
func writeError(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}

func (s *Server) authorized(r *http.Request) bool {
	if s.cfg.APIKey == "" {
		return true
	}
	token := r.Header.Get("X-API-Key")
	if token == "" {
		token = r.URL.Query().Get("api_key")
	}
	return token == s.cfg.APIKey
}

// failed answers an extraction failure and records why. The three causes reach
// the caller as different status codes and call for different responses -- a
// malformed URL is the caller's, an unavailable post is nobody's, a 502 is
// ours -- so counting them under one label made the failure rate undiagnosable.
func (s *Server) failed(w http.ResponseWriter, status int, reason, detail string) {
	s.metrics.Inc("tiktok_extract_results_total", "result", "failed", "reason", reason)
	writeError(w, status, detail)
}

func (s *Server) handleFetch(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		writeError(w, http.StatusUnauthorized, "Unauthorized: invalid or missing API key")
		return
	}
	s.metrics.Inc("tiktok_extract_requests_total")

	target := r.URL.Query().Get("url")
	if r.Method == http.MethodPost {
		var body struct {
			URL string `json:"url"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err == nil && body.URL != "" {
			target = body.URL
		}
	}
	if strings.TrimSpace(target) == "" {
		s.failed(w, http.StatusBadRequest, "bad_url", "URL is required")
		return
	}

	canonical, err := ValidatePostURL(target)
	if err != nil {
		s.failed(w, http.StatusBadRequest, "bad_url", err.Error())
		return
	}

	cacheKey := extractionCacheKey(canonical)
	if cached, ok := s.sessions.GetCached(cacheKey); ok {
		s.metrics.Inc("tiktok_extract_cache_total", "result", "hit")
		w.Header().Set("Content-Type", "application/json")
		w.Write(cached)
		return
	}

	awemeID, err := s.client.ResolveAwemeID(canonical)
	if err != nil {
		s.failed(w, http.StatusBadRequest, "bad_url",
			"TikTok URL must point to a video or photo post")
		return
	}

	item, host, err := s.client.Fetch(awemeID)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			// The API answered and the post is not there: unavailable, private
			// or removed. Deliberately not a 502 -- nothing on our side failed.
			s.failed(w, http.StatusUnprocessableEntity, "unavailable",
				"TikTok video is unavailable or restricted")
			return
		}
		log.Printf("[fetch] %s: %v", awemeID, err)
		s.failed(w, http.StatusBadGateway, "api_error", "Extraction failed: "+err.Error())
		return
	}

	result := s.Build(item, canonical)
	if result == nil {
		s.failed(w, http.StatusUnprocessableEntity, "no_media",
			"TikTok post contains no downloadable media")
		return
	}

	payload, err := json.Marshal(result)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Could not encode result")
		return
	}
	s.sessions.SetCached(cacheKey, payload)
	s.metrics.Inc("tiktok_extract_results_total", "result", "success", "source", extractSource)
	if s.cfg.Verbose {
		log.Printf("[fetch] %s ok via %s (%s)", awemeID, host, result.Status)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(payload)
}

// extractionCacheKey drops tracking parameters so they cannot fork the cache,
// matching how 9111 normalises post URLs.
func extractionCacheKey(canonical string) string {
	u, err := url.Parse(canonical)
	if err != nil {
		return canonical
	}
	lower := strings.ToLower(u.Path)
	if strings.Contains(lower, "/video/") || strings.Contains(lower, "/photo/") ||
		strings.Contains(lower, "/embed/") {
		return (&url.URL{Scheme: u.Scheme, Host: strings.ToLower(u.Host),
			Path: strings.TrimRight(u.Path, "/")}).String()
	}
	return canonical
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":         "healthy",
		"service":        "tiktok-api-engine",
		"uptime_seconds": int64(time.Since(startTime).Seconds()),
	})
}

func (s *Server) handleRoot(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"service":        "tiktok-api-engine",
		"transport":      "go + tiktok app api (aweme/v1/feed)",
		"status":         "ok",
		"uptime_seconds": int64(time.Since(startTime).Seconds()),
	})
}

func (s *Server) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	w.Write([]byte(s.metrics.Render(int64(time.Since(startTime).Seconds()))))
}

func main() {
	cfg := LoadConfig()
	metrics := NewMetrics()
	client, err := NewClient(cfg, metrics)
	if err != nil {
		log.Fatalf("cannot start: %v", err)
	}

	cdnTransport := &http.Transport{
		MaxIdleConnsPerHost: 32,
		IdleConnTimeout:     90 * time.Second,
	}
	if cfg.DownloadProxy != "" {
		p, err := url.Parse(cfg.DownloadProxy)
		if err != nil {
			log.Fatalf("DOWNLOAD_PROXY_URL is not a URL: %v", err)
		}
		cdnTransport.Proxy = http.ProxyURL(p)
	}

	srv := &Server{
		cfg:      cfg,
		client:   client,
		sessions: NewStore(cfg),
		metrics:  metrics,
		// No overall timeout: downloads are long-lived streams and are bounded
		// by the client's context instead.
		cdn: &http.Client{Transport: cdnTransport},
	}

	mux := http.NewServeMux()
	for _, p := range []string{"/fetch", "/tiktok"} {
		mux.HandleFunc(p, srv.handleFetch)
	}
	for _, p := range []string{"/download", "/tiktok/download", "/tunnel"} {
		mux.HandleFunc(p, srv.handleDownload)
	}
	for _, p := range []string{"/health", "/readyz", "/livez"} {
		mux.HandleFunc(p, srv.handleHealth)
	}
	mux.HandleFunc("/metrics", srv.handleMetrics)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			writeError(w, http.StatusNotFound, "Not found")
			return
		}
		srv.handleRoot(w, r)
	})

	addr := cfg.Host + ":" + cfg.Port
	if cfg.APIKey == "" {
		// An unset key disables auth entirely. That is fine behind a private
		// network and dangerous on a public domain, and the difference is
		// invisible until someone else is using the service: an empty
		// TIKTOK_API_KEY in compose looks identical to a configured one.
		log.Printf("WARNING: TIKTOK_API_KEY is empty, so /fetch is open to anyone "+
			"who can reach %s. Downloads leave from this host's IP, so abuse "+
			"lands on it directly. Set the variable unless this port is private.", addr)
	}
	log.Printf("tiktok-api-engine listening on %s (region=%s, %d api hosts, proxy=%q)",
		addr, cfg.Region, len(cfg.APIHosts), cfg.Proxy)
	server := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 15 * time.Second,
	}
	if err := server.ListenAndServe(); err != nil {
		log.Printf("server stopped: %v", err)
		os.Exit(1)
	}
}
