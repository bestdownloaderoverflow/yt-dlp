package delivery

import (
	"strconv"
	"strings"
)

// DeliveryMode represents how content should be delivered to the client.
type DeliveryMode string

const (
	ModeDirect  DeliveryMode = "direct"
	ModeChunked DeliveryMode = "chunked"
	ModeFFmpeg  DeliveryMode = "ffmpeg"
)

const (
	VideoChunkSize = 10 * 1024 * 1024 // 10MB
	AudioChunkSize = 8 * 1024 * 1024  // 8MB
)

// ParseDeliveryPlan converts the IPC response map into a typed struct.
func ParseDeliveryPlan(plan map[string]any) DeliveryPlan {
	return DeliveryPlan{
		DirectURL:        anyString(plan["direct_url"]),
		SourceSize:       int64(anyToInt(plan["source_size"], 0)),
		RequestHeaders:   anyMapToStringMap(plan["request_headers"]),
		ResponseHeaders:  anyMapToStringMap(plan["response_headers"]),
		MediaType:        anyString(plan["media_type"]),
		CanRefresh:       plan["can_refresh"] == true,
		NeedsFFmpeg:      plan["needs_ffmpeg"] == true,
		MergeAV:          plan["ffmpeg_merge"] == true,
		FFmpegAudioURL:   anyString(plan["ffmpeg_audio_url"]),
		FFmpegAudioHdrs:  anyMapToStringMap(plan["ffmpeg_audio_headers"]),
		FFmpegAudioOnly:  plan["ffmpeg_audio_only"] == true,
		Platform:         anyString(plan["platform"]),
		SessionType:      anyString(plan["session_type"]),
		DeliveryMode:     anyString(plan["delivery_mode"]),
		PhotoURLs:        anyToStringSlice(plan["photo_urls"]),
		AudioURL:         anyString(plan["audio_url"]),
		DurationPerImage: anyToInt(plan["duration_per_image"], 4),
		ContentType:      anyString(plan["content_type"]),
		FallbackProxy:    plan["fallback_proxy"] == true,
		FallbackProxyURL: anyString(plan["fallback_proxy_url"]),
		Key:              anyString(plan["key"]),
		UseWorkerMP3:     plan["use_worker_mp3"] == true,
		ProxyURL:         anyString(plan["proxy_url"]),
	}
}

// DeliveryPlan represents a fully parsed plan ready for delivery.
type DeliveryPlan struct {
	DirectURL        string
	SourceSize       int64
	RequestHeaders   map[string]string
	ResponseHeaders  map[string]string
	MediaType        string
	CanRefresh       bool
	NeedsFFmpeg      bool
	MergeAV          bool
	FFmpegAudioURL   string
	FFmpegAudioHdrs  map[string]string
	FFmpegAudioOnly  bool
	Platform         string
	SessionType      string
	DeliveryMode     string
	PhotoURLs        []string // TikTok slideshow photos
	AudioURL         string   // TikTok slideshow audio
	DurationPerImage int      // TikTok slideshow: seconds per image
	ContentType      string   // "slideshow" for TikTok photo posts
	FallbackProxy    bool     // Whether fallback proxy was needed
	FallbackProxyURL string   // SOCKS5 proxy used only if direct media delivery fails
	Key              string   // Session key for refresh
	UseWorkerMP3     bool     // Run MP3 transcode inside worker container
	BypassProxy      bool     // Skip worker proxy; use direct connection
	ProxyURL         string   // SOCKS5 proxy URL for TikTok media delivery
}

// ResolveDeliveryMode decides which delivery mode to use based on the plan and route.
// When the client requested a *-chunked route AND the plan has an exact Content-Length,
// we use chunked mode. Otherwise, ffmpeg requests always win, then direct.
func ResolveDeliveryMode(plan DeliveryPlan, route string) DeliveryMode {
	if plan.NeedsFFmpeg {
		return ModeFFmpeg
	}
	isChunkedRoute := route == "/stream/video-chunked" || route == "/stream/mp3-chunked"
	if isChunkedRoute {
		totalSize := parsePositiveInt64(plan.ResponseHeaders["Content-Length"])
		if totalSize > 0 {
			return ModeChunked
		}
	}
	if plan.DeliveryMode == "single_progressive" {
		if isChunkedRoute {
			return ModeChunked
		}
		return ModeDirect
	}
	return ModeDirect
}

// Utilities (extracted from main.go)
func anyString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func anyMapToStringMap(v any) map[string]string {
	out := map[string]string{}
	obj, ok := v.(map[string]any)
	if !ok {
		return out
	}
	for k, val := range obj {
		if s, ok := val.(string); ok {
			out[k] = s
		}
	}
	return out
}

func parsePositiveInt64(v string) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
	if err != nil || n <= 0 {
		return 0
	}
	return n
}

func parseContentRangeTotal(v string) int64 {
	s := strings.TrimSpace(v)
	if s == "" {
		return 0
	}
	i := strings.LastIndex(s, "/")
	if i < 0 || i+1 >= len(s) {
		return 0
	}
	total := strings.TrimSpace(s[i+1:])
	if total == "" || total == "*" {
		return 0
	}
	return parsePositiveInt64(total)
}

func sanitizeRequestHeaders(headers map[string]string) map[string]string {
	out := make(map[string]string, len(headers)+1)
	for k, v := range headers {
		kl := strings.ToLower(strings.TrimSpace(k))
		if kl == "" || strings.TrimSpace(v) == "" {
			continue
		}
		if kl == "accept-encoding" {
			continue
		}
		out[k] = v
	}
	out["Accept-Encoding"] = "identity"
	return out
}

func ffmpegHeaderString(headers map[string]string) string {
	if len(headers) == 0 {
		return ""
	}
	var b strings.Builder
	for k, v := range headers {
		if strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
			continue
		}
		b.WriteString(k)
		b.WriteString(": ")
		b.WriteString(v)
		b.WriteString("\r\n")
	}
	return b.String()
}

func qualityLabel(raw string) string {
	q := strings.TrimSpace(strings.ToLower(raw))
	if q == "" {
		return "1080p"
	}
	if strings.HasSuffix(q, "p") {
		return q
	}
	if _, err := strconv.Atoi(q); err == nil {
		return q + "p"
	}
	return q
}

func extractKeyFromDownloadLink(link string) string {
	s := strings.TrimSpace(link)
	if s == "" {
		return ""
	}
	const prefix = "/download?key="
	if strings.HasPrefix(s, prefix) {
		return strings.TrimSpace(strings.TrimPrefix(s, prefix))
	}
	if i := strings.Index(s, "key="); i >= 0 {
		return strings.TrimSpace(s[i+4:])
	}
	return s
}

func PickStreamDownloadKey(fetchResult map[string]any, path, quality string) string {
	links, _ := fetchResult["download_links"].(map[string]any)
	if len(links) == 0 {
		return ""
	}
	if path == "/stream/mp3" || path == "/stream/mp3-chunked" || path == "/stream/m4a" {
		if mp3, _ := links["mp3"].(string); mp3 != "" {
			return extractKeyFromDownloadLink(mp3)
		}
		return ""
	}

	videoAny, _ := links["video"].(map[string]any)
	if len(videoAny) == 0 {
		return ""
	}
	target := qualityLabel(quality)
	if linkAny, ok := videoAny[target]; ok {
		if link, _ := linkAny.(string); link != "" {
			return extractKeyFromDownloadLink(link)
		}
	}
	// Fallback to commonly available qualities.
	for _, q := range []string{"1080p", "720p", "480p", "360p", "best"} {
		if linkAny, ok := videoAny[q]; ok {
			if link, _ := linkAny.(string); link != "" {
				return extractKeyFromDownloadLink(link)
			}
		}
	}
	for _, v := range videoAny {
		if link, _ := v.(string); link != "" {
			return extractKeyFromDownloadLink(link)
		}
	}
	return ""
}

func shouldBypassWorkerProxy(plan map[string]any) bool {
	if plan["bypass_proxy"] == true {
		return true
	}
	platform := strings.ToLower(strings.TrimSpace(anyString(plan["platform"])))
	if platform == "tiktok" || platform == "douyin" {
		return true
	}
	if isTikTokMediaURL(anyString(plan["direct_url"])) || isTikTokMediaURL(anyString(plan["ffmpeg_audio_url"])) {
		return true
	}
	for _, u := range anyToStringSlice(plan["photo_urls"]) {
		if isTikTokMediaURL(u) {
			return true
		}
	}
	return false
}

func isTikTokMediaURL(raw string) bool {
	u := strings.ToLower(strings.TrimSpace(raw))
	if u == "" {
		return false
	}
	return strings.Contains(u, "tiktokcdn.com") ||
		strings.Contains(u, "muscdn.com") ||
		strings.Contains(u, "byteoversea.com")
}

func anyToStringSlice(v any) []string {
	if direct, ok := v.([]string); ok {
		out := make([]string, 0, len(direct))
		for _, s := range direct {
			if strings.TrimSpace(s) != "" {
				out = append(out, s)
			}
		}
		return out
	}
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
			out = append(out, s)
		}
	}
	return out
}

func anyToInt(v any, fallback int) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(t))
		if err == nil {
			return n
		}
	}
	return fallback
}

// planToMap converts a DeliveryPlan back to a map for legacy compatibility.
func planToMap(p DeliveryPlan) map[string]any {
	return map[string]any{
		"direct_url":           p.DirectURL,
		"source_size":          p.SourceSize,
		"request_headers":      p.RequestHeaders,
		"response_headers":     p.ResponseHeaders,
		"media_type":           p.MediaType,
		"can_refresh":          p.CanRefresh,
		"needs_ffmpeg":         p.NeedsFFmpeg,
		"platform":             p.Platform,
		"ffmpeg_audio_url":     p.FFmpegAudioURL,
		"ffmpeg_audio_headers": p.FFmpegAudioHdrs,
		"ffmpeg_merge":         p.MergeAV,
		"ffmpeg_audio_only":    p.FFmpegAudioOnly,
		"session_type":         p.SessionType,
		"delivery_mode":        p.DeliveryMode,
		"photo_urls":           p.PhotoURLs,
		"audio_url":            p.AudioURL,
		"duration_per_image":   p.DurationPerImage,
		"content_type":         p.ContentType,
		"fallback_proxy":       p.FallbackProxy,
		"fallback_proxy_url":   p.FallbackProxyURL,
		"key":                  p.Key,
		"use_worker_mp3":       p.UseWorkerMP3,
		"bypass_proxy":         p.BypassProxy,
		"proxy_url":            p.ProxyURL,
	}
}

// npFromMap is the reverse of planToMap.
func npFromMap(m map[string]any) DeliveryPlan {
	return ParseDeliveryPlan(m)
}

type planSourceSelector func(map[string]any) (string, map[string]string)

func selectPrimarySource(plan map[string]any) (string, map[string]string) {
	return strings.TrimSpace(anyString(plan["direct_url"])), anyMapToStringMap(plan["request_headers"])
}

func selectFFmpegAudioSource(plan map[string]any) (string, map[string]string) {
	return strings.TrimSpace(anyString(plan["ffmpeg_audio_url"])), anyMapToStringMap(plan["ffmpeg_audio_headers"])
}
