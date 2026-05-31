package delivery

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type bufferWriteCloser struct {
	bytes.Buffer
}

func (b *bufferWriteCloser) Close() error {
	return nil
}

func TestStreamDirectRangeUsesUpstreamChunkLength(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Range"); got != "bytes=0-3" {
			t.Fatalf("unexpected upstream Range header: %q", got)
		}
		w.Header().Set("Content-Length", "4")
		w.Header().Set("Content-Range", "bytes 0-3/10")
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write([]byte("0123"))
	}))
	defer upstream.Close()

	d := New(DeliveryConfig{}, upstream.Client(), nil)
	plan := DeliveryPlan{
		DirectURL:       upstream.URL,
		ResponseHeaders: map[string]string{"Content-Length": "10"},
		BypassProxy:     true,
	}
	req := httptest.NewRequest(http.MethodGet, "/download", nil)
	req.Header.Set("Range", "bytes=0-3")
	rec := httptest.NewRecorder()

	if ok := d.StreamDirect(rec, req, "", plan, nil); !ok {
		t.Fatal("expected direct stream to succeed")
	}
	resp := rec.Result()
	defer resp.Body.Close()

	if got := resp.StatusCode; got != http.StatusPartialContent {
		t.Fatalf("unexpected status: %d", got)
	}
	if got := resp.Header.Get("Content-Length"); got != "4" {
		t.Fatalf("unexpected Content-Length: %q", got)
	}
	if got := resp.Header.Get("Content-Range"); got != "bytes 0-3/10" {
		t.Fatalf("unexpected Content-Range: %q", got)
	}
	if got := rec.Body.String(); got != "0123" {
		t.Fatalf("unexpected body: %s", fmt.Sprintf("%q", strings.TrimSpace(got)))
	}
}

func TestDownloadToWriterStartsWithRangeRequest(t *testing.T) {
	var ranges []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ranges = append(ranges, r.Header.Get("Range"))
		switch r.Header.Get("Range") {
		case "bytes=0-3":
			w.Header().Set("Content-Range", "bytes 0-3/6")
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write([]byte("0123"))
		case "bytes=4-5":
			w.Header().Set("Content-Range", "bytes 4-5/6")
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write([]byte("45"))
		default:
			t.Fatalf("unexpected Range header: %q", r.Header.Get("Range"))
		}
	}))
	defer upstream.Close()

	d := New(DeliveryConfig{}, upstream.Client(), nil)
	dst := &bufferWriteCloser{}
	plan := DeliveryPlan{DirectURL: upstream.URL, SourceSize: 6}

	if err := d.DownloadToWriter(context.Background(), dst, plan, selectPrimarySource, 4, nil, upstream.Client()); err != nil {
		t.Fatal(err)
	}
	if got := dst.String(); got != "012345" {
		t.Fatalf("unexpected body: %q", got)
	}
	if got := strings.Join(ranges, ","); got != "bytes=0-3,bytes=4-5" {
		t.Fatalf("unexpected ranges: %s", got)
	}
}
