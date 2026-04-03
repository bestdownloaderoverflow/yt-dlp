package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

const ipcPoolSize = 4

var ipcBufReaderPool = sync.Pool{
	New: func() any { return bufio.NewReaderSize(nil, 4096) },
}

type ipcRequest struct {
	ID     string         `json:"id"`
	Method string         `json:"method"`
	Params map[string]any `json:"params,omitempty"`
}

type ipcError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Status  int    `json:"status"`
}

type ipcResponse struct {
	ID     string         `json:"id"`
	OK     bool           `json:"ok"`
	Result map[string]any `json:"result,omitempty"`
	Error  *ipcError      `json:"error,omitempty"`
}

type ExtractorPool struct {
	timeout   time.Duration
	addrs     map[string]string
	workers   []string
	rrCounter atomic.Uint64
	poolMu    sync.Mutex
	connPool  map[string]chan net.Conn
}

func NewExtractorPool(workerCount int, timeout time.Duration) (*ExtractorPool, error) {
	if workerCount < 1 {
		workerCount = 1
	}
	addrs := make(map[string]string, workerCount)
	workers := make([]string, 0, workerCount)
	connPool := make(map[string]chan net.Conn, workerCount)
	for i := 1; i <= workerCount; i++ {
		workerID := fmt.Sprintf("w%d", i)
		addr := fmt.Sprintf("gluetun-%d:9487", i)
		addrs[workerID] = addr
		workers = append(workers, workerID)
		connPool[workerID] = make(chan net.Conn, ipcPoolSize)
	}
	sort.Strings(workers)
	return &ExtractorPool{
		timeout:  timeout,
		addrs:    addrs,
		workers:  workers,
		connPool: connPool,
	}, nil
}

func (p *ExtractorPool) Close() {
	p.poolMu.Lock()
	defer p.poolMu.Unlock()
	for _, ch := range p.connPool {
		close(ch)
		for conn := range ch {
			conn.Close()
		}
	}
}

func (p *ExtractorPool) getConn(workerID string, timeout time.Duration) (net.Conn, error) {
	ch := p.connPool[workerID]
	if ch == nil {
		return nil, fmt.Errorf("unknown worker: %s", workerID)
	}
	// Try to reuse a pooled connection.
	select {
	case conn := <-ch:
		if conn != nil {
			// Validate the connection is still alive by setting a short deadline.
			_ = conn.SetDeadline(time.Now().Add(timeout))
			return conn, nil
		}
	default:
	}
	// Pool empty — dial a new one.
	addr := p.addrs[workerID]
	return net.DialTimeout("tcp", addr, timeout)
}

func (p *ExtractorPool) putConn(workerID string, conn net.Conn) {
	if conn == nil {
		return
	}
	ch := p.connPool[workerID]
	if ch == nil {
		conn.Close()
		return
	}
	select {
	case ch <- conn:
	default:
		// Pool full — discard.
		conn.Close()
	}
}

func (p *ExtractorPool) call(workerID, method string, params map[string]any) (map[string]any, *ipcError, error) {
	return p.callWithTimeout(workerID, method, params, 0)
}

func (p *ExtractorPool) callWithTimeout(workerID, method string, params map[string]any, timeout time.Duration) (map[string]any, *ipcError, error) {
	if timeout <= 0 {
		timeout = p.timeout
	}
	if timeout <= 0 {
		timeout = 45 * time.Second
	}

	conn, err := p.getConn(workerID, timeout)
	if err != nil {
		return nil, nil, err
	}
	_ = conn.SetDeadline(time.Now().Add(timeout))

	reqID := fmt.Sprintf("%s-%d", workerID, p.rrCounter.Add(1))
	req := ipcRequest{ID: reqID, Method: method, Params: params}
	raw, err := json.Marshal(req)
	if err != nil {
		conn.Close()
		return nil, nil, err
	}
	if _, err := conn.Write(append(raw, '\n')); err != nil {
		conn.Close()
		return nil, nil, err
	}

	br := ipcBufReaderPool.Get().(*bufio.Reader)
	br.Reset(conn)
	line, err := br.ReadBytes('\n')
	ipcBufReaderPool.Put(br)
	if err != nil {
		conn.Close()
		return nil, nil, err
	}

	// Return connection to pool after successful read.
	p.putConn(workerID, conn)

	var resp ipcResponse
	if err := json.Unmarshal(line, &resp); err != nil {
		return nil, nil, err
	}
	if resp.ID != reqID {
		return nil, nil, fmt.Errorf("mismatched response id: got=%s want=%s", resp.ID, reqID)
	}
	if !resp.OK {
		if resp.Error == nil {
			return nil, &ipcError{Code: "internal_error", Message: "unknown extractor error", Status: 500}, nil
		}
		return nil, resp.Error, nil
	}
	return resp.Result, nil, nil
}

func (p *ExtractorPool) ExtractInfo(workerID, url, proxy, impersonate string) (map[string]any, *ipcError, error) {
	params := map[string]any{"url": url}
	if proxy != "" {
		params["proxy"] = proxy
	}
	if impersonate != "" {
		params["impersonate"] = impersonate
	}
	return p.call(workerID, "extract_info", params)
}

func (p *ExtractorPool) Fetch(workerID, url, proxy, impersonate string) (map[string]any, *ipcError, error) {
	params := map[string]any{"url": url}
	if proxy != "" {
		params["proxy"] = proxy
	}
	if impersonate != "" {
		params["impersonate"] = impersonate
	}
	return p.call(workerID, "fetch", params)
}

func (p *ExtractorPool) TikTok(workerID, url, proxy, impersonate string) (map[string]any, *ipcError, error) {
	params := map[string]any{"url": url}
	if proxy != "" {
		params["proxy"] = proxy
	}
	if impersonate != "" {
		params["impersonate"] = impersonate
	}
	return p.call(workerID, "tiktok", params)
}

func (p *ExtractorPool) TikTokDownloadPrepare(workerID, key string, download bool) (map[string]any, *ipcError, error) {
	params := map[string]any{
		"key":      key,
		"download": download,
	}
	return p.call(workerID, "tiktok_download_prepare", params)
}

func (p *ExtractorPool) TikTokDownloadRefresh(workerID, key string, download bool) (map[string]any, *ipcError, error) {
	params := map[string]any{
		"key":      key,
		"download": download,
	}
	return p.call(workerID, "tiktok_download_refresh", params)
}

func (p *ExtractorPool) DownloadPrepare(workerID, key string, download bool) (map[string]any, *ipcError, error) {
	params := map[string]any{
		"key":      key,
		"download": download,
	}
	return p.call(workerID, "download_prepare", params)
}

func (p *ExtractorPool) DownloadRefresh(workerID, key string, download bool) (map[string]any, *ipcError, error) {
	params := map[string]any{
		"key":      key,
		"download": download,
	}
	return p.call(workerID, "download_refresh", params)
}

func (p *ExtractorPool) ResolveFormats(workerID, url, formatStr, proxy, impersonate string) (map[string]any, *ipcError, error) {
	params := map[string]any{
		"url":    url,
		"format": formatStr,
	}
	if proxy != "" {
		params["proxy"] = proxy
	}
	if impersonate != "" {
		params["impersonate"] = impersonate
	}
	return p.call(workerID, "resolve_formats", params)
}

func (p *ExtractorPool) Health(workerID string) error {
	_, rpcErr, err := p.callWithTimeout(workerID, "health", nil, 10*time.Second)
	if err != nil {
		return err
	}
	if rpcErr != nil {
		return errors.New(rpcErr.Message)
	}
	return nil
}

func (p *ExtractorPool) PickWorker(preferred string) string {
	if preferred != "" {
		if _, ok := p.addrs[preferred]; ok {
			return preferred
		}
	}
	if len(p.workers) == 0 {
		return ""
	}
	i := int(p.rrCounter.Add(1) % uint64(len(p.workers)))
	return p.workers[i]
}

func (p *ExtractorPool) HasWorker(workerID string) bool {
	_, ok := p.addrs[workerID]
	return ok
}

func (p *ExtractorPool) EnsureHealthy(workerID string) error {
	return p.Health(workerID)
}
