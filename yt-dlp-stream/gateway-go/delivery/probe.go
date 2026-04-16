package delivery

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const directProbeTimeout = 8 * time.Second

// ProbeDirectAccess checks whether gateway-side direct media access is viable.
// It uses a small ranged GET to avoid downloading the full asset before deciding
// whether to stay direct or fall back to the worker proxy/VPN path.
func (d *Delivery) ProbeDirectAccess(ctx context.Context, plan DeliveryPlan) error {
	sources := []struct {
		url     string
		headers map[string]string
		label   string
	}{
		{url: plan.DirectURL, headers: plan.RequestHeaders, label: "primary"},
	}
	if plan.MergeAV && strings.TrimSpace(plan.FFmpegAudioURL) != "" {
		sources = append(sources, struct {
			url     string
			headers map[string]string
			label   string
		}{
			url:     plan.FFmpegAudioURL,
			headers: plan.FFmpegAudioHdrs,
			label:   "audio",
		})
	}

	for _, src := range sources {
		if strings.TrimSpace(src.url) == "" {
			continue
		}
		if err := d.probeDirectURL(ctx, src.url, src.headers); err != nil {
			return fmt.Errorf("%s source: %w", src.label, err)
		}
	}
	return nil
}

func (d *Delivery) probeDirectURL(ctx context.Context, rawURL string, headers map[string]string) error {
	probeCtx, cancel := context.WithTimeout(ctx, directProbeTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, rawURL, nil)
	if err != nil {
		return err
	}
	for k, v := range sanitizeRequestHeaders(headers) {
		req.Header.Set(k, v)
	}
	req.Header.Set("Range", "bytes=0-0")

	resp, err := d.BaseCl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1024))

	switch resp.StatusCode {
	case http.StatusOK, http.StatusPartialContent:
		return nil
	case http.StatusRequestedRangeNotSatisfiable:
		// Treat 416 as usable. Some servers ignore or reject tiny probe ranges
		// even though the underlying media URL is otherwise downloadable.
		return nil
	default:
		return fmt.Errorf("upstream status %d", resp.StatusCode)
	}
}
