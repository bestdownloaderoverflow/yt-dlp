package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"gateway-go/registry"
	"gateway-go/utils"
)

func TestYouTubeFetchRetryableRPCError(t *testing.T) {
	tests := []struct {
		name string
		err  *IPCError
		want bool
	}{
		{
			name: "rate limited code retries",
			err:  &IPCError{Code: "rate_limited", Message: "rate limited", Status: 429},
			want: true,
		},
		{
			name: "upstream dns failure retries",
			err:  &IPCError{Code: "upstream_dns_failure", Message: "temporary failure in name resolution", Status: 502},
			want: true,
		},
		{
			name: "proxy connection failure retries",
			err:  &IPCError{Code: "download_error", Message: "ProxyError: connection reset by peer", Status: 502},
			want: true,
		},
		{
			name: "temporary youtube failure retries",
			err:  &IPCError{Code: "download_error", Message: "HTTP Error 503: Service Unavailable", Status: 503},
			want: true,
		},
		{
			name: "internal downloader failure retries",
			err:  &IPCError{Code: "internal_error", Message: "worker error", Status: 500},
			want: true,
		},
		{
			name: "private video does not retry",
			err:  &IPCError{Code: "download_error", Message: "Private video. Sign in if you've been granted access", Status: 403},
		},
		{
			name: "unavailable video does not retry",
			err:  &IPCError{Code: "download_error", Message: "Video unavailable", Status: 404},
		},
		{
			name: "unsupported url does not retry",
			err:  &IPCError{Code: "bad_request", Message: "Unsupported URL", Status: 400},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := isRetryableYouTubeFetchRPCError(tt.err)
			if got != tt.want {
				t.Fatalf("retryable=%v, want %v", got, tt.want)
			}
		})
	}
}

type youtubeFetchFailoverExtractor struct {
	calls   int
	proxies []string
}

func (e *youtubeFetchFailoverExtractor) ExtractInfo(workerID, url, proxy, impersonate string) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) Fetch(workerID, url, proxy, impersonate string, forceIPv6 bool) (map[string]any, *IPCError, error) {
	e.calls++
	e.proxies = append(e.proxies, proxy)
	if e.calls == 1 {
		return nil, &IPCError{Code: "download_error", Message: "ProxyError: connection reset by peer", Status: 502}, nil
	}
	return map[string]any{"platform": "youtube", "id": "ok"}, nil, nil
}

func (e *youtubeFetchFailoverExtractor) TikTok(workerID, url, proxy, impersonate string) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) TikTokDownloadPrepare(workerID, key string, download bool) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) TikTokDownloadRefresh(workerID, key string, download bool) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) DownloadPrepare(workerID, key string, download bool) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) DownloadRefresh(workerID, key string, download bool) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) ResolveFormats(workerID, url, format, proxy, impersonate string) (map[string]any, *IPCError, error) {
	return nil, nil, nil
}

func (e *youtubeFetchFailoverExtractor) Health(workerID string) error {
	return nil
}

func (e *youtubeFetchFailoverExtractor) PickWorker(preferred string) string {
	if preferred != "" {
		return preferred
	}
	return "w1"
}

func (e *youtubeFetchFailoverExtractor) HasWorker(workerID string) bool {
	return workerID == "w1"
}

func (e *youtubeFetchFailoverExtractor) InFlight(workerID string) int {
	return 0
}

func TestYouTubeFetchFailsOverToAnotherProxy(t *testing.T) {
	proxyReg := registry.NewProxyRegistry(registry.ProxyConfig{
		ProxyCount:             2,
		ProxyCooldownSeconds:   300,
		HealthFailureThreshold: 1,
	})
	proxyReg.SetHealthy("p1", true, "203.0.113.1")
	proxyReg.SetHealthy("p2", true, "203.0.113.2")

	extractor := &youtubeFetchFailoverExtractor{}
	h := &Handlers{
		Config: utils.Config{
			WorkerCount:         1,
			MaxRetries:          2,
			DegradedRetryAfter:  5,
			ExtractorIPCEnabled: true,
		},
		Registry:  registry.NewWorkerRegistry(registry.Config{WorkerCount: 1}),
		ProxyReg:  proxyReg,
		Extractor: extractor,
	}

	req := httptest.NewRequest(http.MethodGet, "/fetch?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DZ7DaHyJnVN4", nil)
	rec := httptest.NewRecorder()
	h.handleFetchViaExtractorIPC(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if extractor.calls != 2 {
		t.Fatalf("calls=%d, want 2", extractor.calls)
	}
	if len(extractor.proxies) != 2 || extractor.proxies[0] == "" || extractor.proxies[1] == "" {
		t.Fatalf("proxies=%#v, want two non-empty proxy args", extractor.proxies)
	}
	if extractor.proxies[0] == extractor.proxies[1] {
		t.Fatalf("proxy did not fail over: %#v", extractor.proxies)
	}

	var payload map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("invalid json response: %v", err)
	}
	if payload["platform"] != "youtube" {
		t.Fatalf("platform=%v, want youtube", payload["platform"])
	}
}
