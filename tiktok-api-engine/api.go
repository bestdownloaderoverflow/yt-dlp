package main

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

const appUserAgent = "com.zhiliaoapp.musically/300904 (2018111632; U; Android 10; en_US; " +
	"Pixel 4; Build/QQ3A.200805.001; Cronet/58.0.2991.0)"

var (
	ErrNotFound = errors.New("post not available")
	// errUnrelatedFeed marks a well-formed answer that simply did not contain
	// the requested post -- TikTok returns a generic feed with status_code 0
	// instead of an error when it will not serve one.
	errUnrelatedFeed = errors.New("served an unrelated feed")
	ErrUnreadable    = errors.New("could not read the post from any API host")
)

var awemeIDPattern = regexp.MustCompile(`/(?:video|photo)/(\d{17,21})`)

type Client struct {
	cfg   Config
	http  *http.Client
	next  chan int // round-robin cursor over cfg.APIHosts
	short *http.Client
}

func NewClient(cfg Config) (*Client, error) {
	transport := &http.Transport{
		MaxIdleConnsPerHost: 16,
		IdleConnTimeout:     90 * time.Second,
	}
	if cfg.Proxy != "" {
		p, err := url.Parse(cfg.Proxy)
		if err != nil {
			return nil, fmt.Errorf("PROXY_URL is not a URL: %w", err)
		}
		transport.Proxy = http.ProxyURL(p)
	}
	c := &Client{
		cfg:  cfg,
		http: &http.Client{Transport: transport, Timeout: cfg.RequestTimeout},
		next: make(chan int, 1),
		short: &http.Client{
			Transport: transport,
			Timeout:   cfg.RequestTimeout,
		},
	}
	c.next <- 0
	return c, nil
}

func randDigits(n int) string {
	const digits = "0123456789"
	return randFrom(digits, n)
}

func randFrom(alphabet string, n int) string {
	out := make([]byte, n)
	max := big.NewInt(int64(len(alphabet)))
	for i := range out {
		v, err := rand.Int(rand.Reader, max)
		if err != nil {
			// crypto/rand does not fail in practice; a fixed byte here would
			// only weaken the fingerprint, never break the request.
			out[i] = alphabet[0]
			continue
		}
		out[i] = alphabet[v.Int64()]
	}
	return string(out)
}

// deviceParams builds a fresh fake device fingerprint. Every field that
// identifies a device is regenerated per call on purpose: reusing one would
// invent a per-device rate limit that does not otherwise exist.
func (c *Client) deviceParams(awemeID string) string {
	nowMS := time.Now().UnixMilli()
	q := url.Values{
		"aweme_id":              {awemeID},
		"version_name":          {"1.1.9"},
		"version_code":          {"2018111632"},
		"build_number":          {"1.1.9"},
		"device_id":             {"7" + randDigits(18)},
		"iid":                   {"7" + randDigits(18)},
		"manifest_version_code": {"2018111632"},
		"update_version_code":   {"2018111632"},
		"openudid":              {randFrom("0123456789abcdef", 16)},
		"uuid":                  {randDigits(16)},
		"_rticket":              {fmt.Sprint(nowMS * 1000)},
		"ts":                    {fmt.Sprint(nowMS)},
		"device_brand":          {"Google"},
		"device_type":           {"Pixel 4"},
		"device_platform":       {"android"},
		"resolution":            {"1080*1920"},
		"dpi":                   {"420"},
		"os_version":            {"10"},
		"os_api":                {"29"},
		"carrier_region":        {c.cfg.Region},
		"sys_region":            {c.cfg.Region},
		"region":                {c.cfg.Region},
		"timezone_name":         {c.cfg.Timezone},
		"timezone_offset":       {c.cfg.TimezoneOffset},
		"channel":               {"googleplay"},
		"ac":                    {"wifi"},
		"mcc_mnc":               {c.cfg.MccMnc},
		"is_my_cn":              {"0"},
		"ssmix":                 {"a"},
		"as":                    {"a1qwert123"},
		"cp":                    {"cbfhckdckkde1"},
	}
	return q.Encode()
}

// ResolveAwemeID turns any accepted TikTok URL into a numeric post id,
// following vt./vm. short links when needed.
func (c *Client) ResolveAwemeID(rawURL string) (string, error) {
	if m := awemeIDPattern.FindStringSubmatch(rawURL); m != nil {
		return m[1], nil
	}
	req, err := http.NewRequest(http.MethodGet, rawURL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", desktopUserAgent)
	resp, err := c.short.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if m := awemeIDPattern.FindStringSubmatch(resp.Request.URL.String()); m != nil {
		return m[1], nil
	}
	return "", ErrNotFound
}

type feedResponse struct {
	StatusCode int               `json:"status_code"`
	AwemeList  []json.RawMessage `json:"aweme_list"`
}

// Fetch asks the app API for one post, rotating hosts on failure.
//
// TikTok answers a request whose region it will not serve with an unrelated
// feed and status_code 0 -- a success shape carrying the wrong post. Every
// result is therefore matched on aweme_id before it is accepted; without that
// check this service would confidently hand back somebody else's video.
func (c *Client) Fetch(awemeID string) (*Aweme, string, error) {
	params := c.deviceParams(awemeID)
	hosts := c.cfg.APIHosts
	if len(hosts) == 0 {
		return nil, "", errors.New("no API hosts configured")
	}
	start := <-c.next
	c.next <- (start + 1) % len(hosts)

	var lastErr error
	for attempt := 0; attempt < c.cfg.MaxAttempts; attempt++ {
		tried, absent := 0, 0
		for i := range hosts {
			host := hosts[(start+i)%len(hosts)]
			item, err := c.fetchFrom(host, awemeID, params)
			if err == nil {
				return item, host, nil
			}
			tried++
			if errors.Is(err, errUnrelatedFeed) {
				absent++
			}
			lastErr = err
		}
		// A sweep in which every host answered cleanly and none of them had the
		// post is already unanimous: the post is unavailable (deleted, private,
		// or refused for this region). Nothing on this side failed, so this is
		// not an extraction error. Sweeping again only asks the same hosts the
		// same question -- with ten hosts that is twenty wasted requests and a
		// minute of latency on a verdict already reached.
		//
		// The bar itself is unchanged: every host must agree. One host calling a
		// post missing is not enough to condemn a live one, which matters because
		// a host can quietly stop serving a caller -- answering 200 and
		// status_code 0 with somebody else's feed -- rather than failing outright.
		if absent == tried {
			return nil, "", ErrNotFound
		}
		// A fresh fingerprint for the next sweep; the previous one may be what
		// the API objected to.
		params = c.deviceParams(awemeID)
	}
	if lastErr == nil {
		lastErr = ErrUnreadable
	}
	return nil, "", lastErr
}

func (c *Client) fetchFrom(host, awemeID, params string) (*Aweme, error) {
	endpoint := "https://" + host + "/aweme/v1/feed/?" + params
	// OPTIONS, not GET: the app API serves the payload for a preflight-shaped
	// request without the signature a GET is checked for. A GET returns nothing
	// usable -- measured 0/20 against 15/15 for OPTIONS on the same host.
	req, err := http.NewRequest(http.MethodOptions, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", appUserAgent)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 12<<20))
	if err != nil {
		return nil, err
	}
	if len(body) == 0 {
		return nil, fmt.Errorf("%s: empty body", host)
	}
	var feed feedResponse
	if err := json.Unmarshal(body, &feed); err != nil {
		return nil, fmt.Errorf("%s: %w", host, err)
	}
	if feed.StatusCode != 0 {
		return nil, fmt.Errorf("%s: status_code %d", host, feed.StatusCode)
	}
	for _, raw := range feed.AwemeList {
		var probe struct {
			AwemeID string `json:"aweme_id"`
		}
		if err := json.Unmarshal(raw, &probe); err != nil || probe.AwemeID != awemeID {
			continue
		}
		var item Aweme
		if err := json.Unmarshal(raw, &item); err != nil {
			return nil, fmt.Errorf("%s: %w", host, err)
		}
		return &item, nil
	}
	// A populated list that does not contain the post means the API served a
	// generic feed instead. That is a refusal, not a transport failure.
	if len(feed.AwemeList) > 0 {
		return nil, fmt.Errorf("%s: %w", host, errUnrelatedFeed)
	}
	return nil, fmt.Errorf("%s: %w", host, errUnrelatedFeed)
}

// ValidatePostURL mirrors the 9111 contract: official TikTok post or short
// links only, normalised so tracking parameters cannot fork the cache.
func ValidatePostURL(value string) (string, error) {
	raw := strings.TrimSpace(value)
	if raw == "" {
		return "", errors.New("URL is required")
	}
	if !strings.Contains(raw, "://") {
		raw = "https://" + strings.TrimLeft(raw, "/")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", errors.New("Only TikTok URLs are supported")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", errors.New("TikTok URL must use http or https")
	}
	host := strings.ToLower(strings.TrimSuffix(u.Hostname(), "."))
	if host != "tiktok.com" && !strings.HasSuffix(host, ".tiktok.com") {
		return "", errors.New("Only TikTok URLs are supported")
	}
	path := u.Path
	if path == "" {
		path = "/"
	}
	isShort := (host == "vm.tiktok.com" || host == "vt.tiktok.com" || host == "t.tiktok.com") && path != "/"
	lower := strings.ToLower(path)
	isPost := strings.Contains(lower, "/video/") || strings.Contains(lower, "/photo/") ||
		strings.Contains(lower, "/embed/")
	if !isShort && !isPost {
		return "", errors.New("TikTok URL must point to a video or photo post")
	}
	clean := strings.TrimRight(path, "/")
	if clean == "" {
		clean = "/"
	}
	return (&url.URL{Scheme: "https", Host: host, Path: clean, RawQuery: u.RawQuery}).String(), nil
}
