package main

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// Hosts that returned the requested aweme_id on every attempt, measured at
// 15/15 and re-checked at 8/8 an hour later, then confirmed against a second
// video and a photo post. Ordered so the five long-serving hosts answer first
// and the newer ones act as depth behind them.
//
// The spread is the point, not the count. Every host here except the .eu and
// .us entries lives in useast2a behind Fastly; no1a, ie and useastred are
// separate European clusters, and useast8 is Akamai. A rotation that is all
// one cluster is one outage away from being no rotation at all.
//
// Excluded on purpose: useast1a, eu1a and the non-Singapore ali* clusters
// answer nothing. alisg answers 4-10 of 15. useast5 is the cautionary one --
// it measured 14/15, then 1/15 an hour later while these held, serving a feed
// of other videos rather than the one asked for. Hosts can be withdrawn from a
// caller without any error to notice, which is why absence is verified against
// every host before a post is called unavailable.
var defaultAPIHosts = []string{
	"api19-normal-c-useast2a.tiktokv.com",
	"api16-normal-c-useast2a.tiktokv.com",
	"api22-normal-c-useast2a.tiktokv.com",
	"api19-normal-useast2a.tiktokv.com",
	"api16-core-c-useast2a.tiktokv.com",
	"api16-normal-useast8.tiktokv.us",
	"api16-core-useast8.tiktokv.us",
	"api19-normal-useast8.tiktokv.us",
	"api16-normal-no1a.tiktokv.eu",
	"api19-normal-no1a.tiktokv.eu",
	"api16-normal-ie.tiktokv.eu",
	"api16-core-ie.tiktokv.eu",
	"api16-normal-useastred.tiktokv.eu",
}

type Config struct {
	Host    string
	Port    string
	APIKey  string
	Verbose bool

	RedisURL   string
	SessionTTL time.Duration
	CacheTTL   time.Duration

	APIHosts       []string
	Region         string
	Timezone       string
	TimezoneOffset string
	MccMnc         string
	MaxAttempts    int
	RequestTimeout time.Duration

	// Empty means direct. TikTok's app API is not reachable over IPv6 (no AAAA
	// on any host, checked against three resolvers), so a proxy here is the only
	// way to vary the source address if that ever becomes necessary.
	Proxy         string
	DownloadProxy string

	SlideshowSecondsPerImage int
	SlideshowMaxImages       int
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v, err := strconv.Atoi(os.Getenv(key)); err == nil && v > 0 {
		return v
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	switch strings.ToLower(os.Getenv(key)) {
	case "1", "true", "yes":
		return true
	case "0", "false", "no":
		return false
	}
	return fallback
}

func LoadConfig() Config {
	hosts := defaultAPIHosts
	if raw := os.Getenv("TIKTOK_API_HOSTS"); raw != "" {
		hosts = nil
		for _, h := range strings.Split(raw, ",") {
			if h = strings.TrimSpace(h); h != "" {
				hosts = append(hosts, h)
			}
		}
	}
	return Config{
		Host:    env("HOST", "0.0.0.0"),
		Port:    env("PORT", "9110"),
		APIKey:  os.Getenv("TIKTOK_API_KEY"),
		Verbose: envBool("VERBOSE_LOGS", false),

		RedisURL:   env("REDIS_URL", ""),
		SessionTTL: time.Duration(envInt("SESSION_TTL", 3600)) * time.Second,
		CacheTTL:   time.Duration(envInt("CACHE_TTL", 300)) * time.Second,

		APIHosts: hosts,
		// The region parameter, not the exit IP, is what decides whether TikTok
		// serves geo-restricted content: measured from Singapore and Thailand
		// exits, region=ID returned the post and region=US returned an
		// unrelated feed. That is why this service needs no Indonesian exit.
		Region:         env("TIKTOK_REGION", "ID"),
		Timezone:       env("TIKTOK_TIMEZONE", "Asia/Jakarta"),
		TimezoneOffset: env("TIKTOK_TIMEZONE_OFFSET", "25200"),
		MccMnc:         env("TIKTOK_MCC_MNC", "51010"),
		MaxAttempts:    envInt("MAX_ATTEMPTS", 3),
		RequestTimeout: time.Duration(envInt("REQUEST_TIMEOUT", 15)) * time.Second,

		Proxy:         os.Getenv("PROXY_URL"),
		DownloadProxy: os.Getenv("DOWNLOAD_PROXY_URL"),

		SlideshowSecondsPerImage: envInt("SLIDESHOW_SECONDS_PER_IMAGE", 3),
		SlideshowMaxImages:       envInt("SLIDESHOW_MAX_IMAGES", 40),
	}
}
