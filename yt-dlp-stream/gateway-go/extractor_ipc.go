package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sort"
	"sync/atomic"
	"time"
)

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
	rrCounter atomic.Uint64 // used for request IDs only
	rwCounter atomic.Uint64 // used for round-robin worker selection only
}

func NewExtractorPool(workerCount int, timeout time.Duration) (*ExtractorPool, error) {
	if workerCount < 1 {
		workerCount = 1
	}
	addrs := make(map[string]string, workerCount)
	workers := make([]string, 0, workerCount)
	for i := 1; i <= workerCount; i++ {
		workerID := fmt.Sprintf("w%d", i)
		addr := fmt.Sprintf("gluetun-%d:9487", i)
		addrs[workerID] = addr
		workers = append(workers, workerID)
	}
	sort.Strings(workers)
	return &ExtractorPool{
		timeout: timeout,
		addrs:   addrs,
		workers: workers,
	}, nil
}

func (p *ExtractorPool) Close() {}

func (p *ExtractorPool) call(workerID, method string, params map[string]any) (map[string]any, *ipcError, error) {
	addr, ok := p.addrs[workerID]
	if !ok {
		return nil, nil, fmt.Errorf("unknown worker: %s", workerID)
	}

	timeout := p.timeout
	if timeout <= 0 {
		timeout = 45 * time.Second
	}

	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return nil, nil, err
	}
	defer conn.Close()

	_ = conn.SetDeadline(time.Now().Add(timeout))

	reqID := fmt.Sprintf("%s-%d", workerID, p.rrCounter.Add(1))
	req := ipcRequest{ID: reqID, Method: method, Params: params}
	raw, err := json.Marshal(req)
	if err != nil {
		return nil, nil, err
	}
	if _, err := conn.Write(append(raw, '\n')); err != nil {
		return nil, nil, err
	}

	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil {
		return nil, nil, err
	}

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
	_, rpcErr, err := p.call(workerID, "health", nil)
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
	i := int(p.rwCounter.Add(1) % uint64(len(p.workers)))
	return p.workers[i]
}

func (p *ExtractorPool) HasWorker(workerID string) bool {
	_, ok := p.addrs[workerID]
	return ok
}

