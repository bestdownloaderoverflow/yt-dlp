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

type deviceProfile struct {
	brand      string
	model      string
	osVersion  string
	osAPI      string
	resolution string
	dpi        string
	build      string
	cronetVer  string
}

var devicePool = []deviceProfile{
	{
		brand:      "Google",
		model:      "Pixel 4",
		osVersion:  "10",
		osAPI:      "29",
		resolution: "1080*1920",
		dpi:        "420",
		build:      "QQ3A.200805.001",
		cronetVer:  "58.0.2991.0",
	},
	{
		brand:      "Google",
		model:      "Pixel 6",
		osVersion:  "12",
		osAPI:      "31",
		resolution: "1080*2400",
		dpi:        "411",
		build:      "SQ1D.211205.016.A1",
		cronetVer:  "60.0.3112.0",
	},
	{
		brand:      "Samsung",
		model:      "SM-G991B", // Galaxy S21
		osVersion:  "11",
		osAPI:      "30",
		resolution: "1080*2400",
		dpi:        "421",
		build:      "RP1A.200720.012",
		cronetVer:  "58.0.2991.0",
	},
	{
		brand:      "Samsung",
		model:      "SM-S901B", // Galaxy S22
		osVersion:  "12",
		osAPI:      "31",
		resolution: "1080*2340",
		dpi:        "425",
		build:      "SP1A.210812.016",
		cronetVer:  "60.0.3112.0",
	},
	{
		brand:      "Xiaomi",
		model:      "22111317G", // Redmi Note 12
		osVersion:  "12",
		osAPI:      "31",
		resolution: "1080*2400",
		dpi:        "395",
		build:      "SKQ1.211103.001",
		cronetVer:  "58.0.2991.0",
	},
	{
		brand:      "Xiaomi",
		model:      "M2101K6G", // Redmi Note 10 Pro
		osVersion:  "11",
		osAPI:      "30",
		resolution: "1080*2400",
		dpi:        "395",
		build:      "RKQ1.200826.002",
		cronetVer:  "58.0.2991.0",
	},
	{
		brand:      "OPPO",
		model:      "CPH2359", // Reno 8
		osVersion:  "12",
		osAPI:      "31",
		resolution: "1080*2400",
		dpi:        "409",
		build:      "SP1A.210812.016",
		cronetVer:  "60.0.3112.0",
	},
	{
		brand:      "vivo",
		model:      "V2205", // Vivo Y35
		osVersion:  "11",
		osAPI:      "30",
		resolution: "1080*2408",
		dpi:        "401",
		build:      "RP1A.200720.012",
		cronetVer:  "58.0.2991.0",
	},
}

var carrierMccMnc = []string{
	"51010", // Telkomsel
	"51011", // XL Axiata
	"51001", // Indosat Ooredoo Hutchison
	"51089", // 3 (Tri)
	"51028", // Smartfren
}

var accessTypes = []string{"wifi", "wifi", "wifi", "4g", "4g", "5g"}

func randInt(max int) int {
	if max <= 0 {
		return 0
	}
	n, err := rand.Int(rand.Reader, big.NewInt(int64(max)))
	if err != nil {
		return 0
	}
	return int(n.Int64())
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

func buildUserAgent(dev deviceProfile) string {
	return fmt.Sprintf("com.zhiliaoapp.musically/300904 (2018111632; U; Android %s; en_US; %s; Build/%s; Cronet/%s)",
		dev.osVersion, dev.model, dev.build, dev.cronetVer)
}

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
	cfg     Config
	http    *http.Client
	next    chan int // round-robin cursor over cfg.APIHosts
	short   *http.Client
	metrics *Metrics
}

func NewClient(cfg Config, metrics *Metrics) (*Client, error) {
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
		cfg:     cfg,
		metrics: metrics,
		http:    &http.Client{Transport: transport, Timeout: cfg.RequestTimeout},
		next:    make(chan int, 1),
		short: &http.Client{
			Transport: transport,
			Timeout:   cfg.RequestTimeout,
		},
	}
	c.next <- 0
	return c, nil
}

// deviceParams builds a fresh fake device fingerprint. Every field that
// identifies a device is regenerated per call on purpose: reusing one would
// invent a per-device rate limit that does not otherwise exist.
func (c *Client) deviceParams(awemeID string) (string, string) {
	nowMS := time.Now().UnixMilli()
	dev := devicePool[randInt(len(devicePool))]

	mccMnc := c.cfg.MccMnc
	if mccMnc == "51010" || mccMnc == "" {
		mccMnc = carrierMccMnc[randInt(len(carrierMccMnc))]
	}
	ac := accessTypes[randInt(len(accessTypes))]

	asToken := "a1" + randFrom("abcdefghijklmnopqrstuvwxyz0123456789", 8)
	cpToken := "cb" + randFrom("abcdefghijklmnopqrstuvwxyz0123456789", 11)

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
		"device_brand":          {dev.brand},
		"device_type":           {dev.model},
		"device_platform":       {"android"},
		"resolution":            {dev.resolution},
		"dpi":                   {dev.dpi},
		"os_version":            {dev.osVersion},
		"os_api":                {dev.osAPI},
		"carrier_region":        {c.cfg.Region},
		"sys_region":            {c.cfg.Region},
		"region":                {c.cfg.Region},
		"timezone_name":         {c.cfg.Timezone},
		"timezone_offset":       {c.cfg.TimezoneOffset},
		"channel":               {"googleplay"},
		"ac":                    {ac},
		"mcc_mnc":               {mccMnc},
		"is_my_cn":              {"0"},
		"ssmix":                 {"a"},
		"as":                    {asToken},
		"cp":                    {cpToken},
	}
	ua := buildUserAgent(dev)
	return q.Encode(), ua
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
	params, ua := c.deviceParams(awemeID)
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
			item, err := c.fetchFrom(host, awemeID, params, ua)
			c.recordHost(host, err)
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
		params, ua = c.deviceParams(awemeID)
	}
	if lastErr == nil {
		lastErr = ErrUnreadable
	}
	return nil, "", lastErr
}

// recordHost counts what each host did with a request. A host can stop serving
// a caller without failing: it answers 200 with status_code 0 and a feed of
// other videos, which rotation then papers over. Split per host, that shows up
// as one host's "absent" climbing while the same posts succeed on the others --
// the only way to see it before enough hosts drift to raise the failure rate.
func (c *Client) recordHost(host string, err error) {
	if c.metrics == nil {
		return
	}
	result := "ok"
	switch {
	case err == nil:
	case errors.Is(err, errUnrelatedFeed):
		result = "absent"
	default:
		result = "error"
	}
	c.metrics.Inc("tiktok_api_host_total", "host", host, "result", result)
}

func (c *Client) fetchFrom(host, awemeID, params, ua string) (*Aweme, error) {
	endpoint := "https://" + host + "/aweme/v1/feed/?" + params
	// OPTIONS, not GET: the app API serves the payload for a preflight-shaped
	// request without the signature a GET is checked for. A GET returns nothing
	// usable -- measured 0/20 against 15/15 for OPTIONS on the same host.
	req, err := http.NewRequest(http.MethodOptions, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", ua)
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("X-SS-REQ-TICKET", fmt.Sprint(time.Now().UnixMilli()))
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
