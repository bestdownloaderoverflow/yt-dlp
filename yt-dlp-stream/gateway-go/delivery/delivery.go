// Package delivery handles streaming orchestration: direct pass-through,
// HTTP Range chunked streaming, ffmpeg-based transcoding/merging, and
// TikTok photo slideshow rendering.
//
// IPC to Python is used ONLY for extraction/metadata. All streaming
// logic lives in Go.
package delivery

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

type Extractor interface {
	DownloadRefresh(workerID, key string, download bool) (map[string]any, error)
	TikTokDownloadRefresh(workerID, key string, download bool) (map[string]any, error)
}

type WorkerLookup interface {
	GetWorker(workerID string) *Worker
}

type Worker struct {
	Host      string
	ProxyPort int
}

func (w *Worker) HTTPProxyURL() string {
	return fmt.Sprintf("http://%s:%d", w.Host, w.ProxyPort)
}

type Delivery struct {
	Config       DeliveryConfig
	Worker       WorkerLookup
	BaseCl       *http.Client
	proxyClients sync.Map // map[string]*http.Client keyed by proxy URL
}

type DeliveryConfig struct {
	DegradedRetryAfter int
}

func New(cfg DeliveryConfig, baseCl *http.Client, worker WorkerLookup) *Delivery {
	return &Delivery{Config: cfg, BaseCl: baseCl, Worker: worker}
}

func (d *Delivery) mediaHTTPClient(workerID string) *http.Client {
	if workerID == "" {
		return d.BaseCl
	}
	worker := d.Worker.GetWorker(workerID)
	if worker == nil || worker.Host == "" || worker.ProxyPort <= 0 {
		return d.BaseCl
	}
	proxyURLStr := worker.HTTPProxyURL()

	// Check cache first.
	if cached, ok := d.proxyClients.Load(proxyURLStr); ok {
		return cached.(*http.Client)
	}

	baseTransport, ok := d.BaseCl.Transport.(*http.Transport)
	if !ok || baseTransport == nil {
		return d.BaseCl
	}
	proxyURL, err := url.Parse(proxyURLStr)
	if err != nil {
		return d.BaseCl
	}
	tr := baseTransport.Clone()
	tr.Proxy = http.ProxyURL(proxyURL)
	cl := &http.Client{
		Transport: tr,
		Timeout:   d.BaseCl.Timeout,
	}

	// Store in cache (or return existing if raced).
	if existing, loaded := d.proxyClients.LoadOrStore(proxyURLStr, cl); loaded {
		return existing.(*http.Client)
	}
	return cl
}

func (d *Delivery) mediaHTTPClientForPlan(workerID string, plan map[string]any) *http.Client {
	if shouldBypassWorkerProxy(plan) {
		return d.BaseCl
	}
	return d.mediaHTTPClient(workerID)
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
		strings.Contains(msg, "context canceled")
}

func CopyHeader(dst, src http.Header, skip map[string]bool) {
	for k, values := range src {
		if skip[strings.ToLower(k)] {
			continue
		}
		for _, v := range values {
			dst.Add(k, v)
		}
	}
}

func WriteJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func positiveSeconds(d time.Duration) int {
	if d <= 0 {
		return 0
	}
	return int(d.Seconds())
}
