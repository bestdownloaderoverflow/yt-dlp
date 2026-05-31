// Package delivery handles streaming orchestration: direct pass-through,
// HTTP Range chunked streaming, ffmpeg-based transcoding/merging, and
// TikTok photo slideshow rendering.
//
// IPC to Python is used ONLY for extraction/metadata. All streaming
// logic lives in Go.
package delivery

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	neturl "net/url"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/proxy"
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
	Config        DeliveryConfig
	Worker        WorkerLookup
	BaseCl        *http.Client
	proxyClients  sync.Map // map[string]*http.Client keyed by HTTP proxy URL
	socks5Clients sync.Map // map[string]*http.Client keyed by SOCKS5 proxy URL
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
	proxyURL, err := neturl.Parse(proxyURLStr)
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

func (d *Delivery) mediaSOCKS5Client(socksURL string) *http.Client {
	if socksURL == "" {
		return d.BaseCl
	}

	if cached, ok := d.socks5Clients.Load(socksURL); ok {
		return cached.(*http.Client)
	}

	u, err := neturl.Parse(socksURL)
	if err != nil {
		return d.BaseCl
	}
	host, port, err := net.SplitHostPort(u.Host)
	if err != nil {
		addr := u.Host + ":1080"
		host, port, err = net.SplitHostPort(addr)
		if err != nil {
			return d.BaseCl
		}
	}

	socksDialer, err := proxy.SOCKS5("tcp", net.JoinHostPort(host, port), nil, proxy.Direct)
	if err != nil {
		return d.BaseCl
	}

	baseTransport, ok := d.BaseCl.Transport.(*http.Transport)
	if !ok || baseTransport == nil {
		baseTransport = &http.Transport{
			MaxIdleConns:        512,
			MaxIdleConnsPerHost: 128,
			IdleConnTimeout:     90 * time.Second,
			DisableCompression:  false,
		}
	}

	tr := baseTransport.Clone()
	if tr.TLSClientConfig == nil {
		tr.TLSClientConfig = &tls.Config{}
	} else {
		tr.TLSClientConfig = tr.TLSClientConfig.Clone()
	}
	tr.TLSClientConfig.NextProtos = []string{"http/1.1"}
	tr.ForceAttemptHTTP2 = false
	tr.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		if contextDialer, ok := socksDialer.(proxy.ContextDialer); ok {
			return contextDialer.DialContext(ctx, network, addr)
		}
		return socksDialer.Dial(network, addr)
	}
	tr.Proxy = nil

	cl := &http.Client{
		Transport: tr,
		Timeout:   d.BaseCl.Timeout,
	}

	if existing, loaded := d.socks5Clients.LoadOrStore(socksURL, cl); loaded {
		return existing.(*http.Client)
	}
	return cl
}

func (d *Delivery) PurgeSOCKS5Client(socksURL string) {
	d.socks5Clients.Delete(socksURL)
}

// PurgeWorkerClient removes the cached proxy client for a worker.
// Call this before restarting the worker's gluetun container so that
// stale TCP connections to the old VPN network namespace are discarded.
func (d *Delivery) PurgeWorkerClient(workerID string) {
	worker := d.Worker.GetWorker(workerID)
	if worker == nil || worker.Host == "" || worker.ProxyPort <= 0 {
		return
	}
	d.proxyClients.Delete(worker.HTTPProxyURL())
}

func (d *Delivery) mediaHTTPClientForPlan(workerID string, plan map[string]any) *http.Client {
	proxyURL := anyString(plan["proxy_url"])
	if proxyURL != "" {
		return d.mediaSOCKS5Client(proxyURL)
	}
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
		strings.Contains(msg, "context canceled") ||
		strings.Contains(msg, "use of closed network connection") ||
		strings.Contains(msg, "unexpected eof") ||
		strings.Contains(msg, "http2: server sent goaway") ||
		strings.Contains(msg, "client disconnected") ||
		strings.Contains(msg, "tls: error decoding message") ||
		strings.Contains(msg, "stream closed")
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
