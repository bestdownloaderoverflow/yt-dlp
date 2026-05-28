package utils

import (
	"os"
	"strconv"
	"strings"
)

type Config struct {
	GatewayPort               int
	WorkerCount               int
	ExtractorIPCEnabled       bool
	YouTubeFetchIPv6Only      bool
	YouTubeFetchWorker        string
	YouTubeFetchWorkers       []string
	ExtractorPythonBin        string
	ExtractorWorkerPath       string
	ExtractorTimeoutMs        int
	DegradedTimeoutSeconds    int
	GluetunPassword           string
	MaxRetries                int
	TikTokAPIKey              string
	HealthCheckTimeoutMs      int
	HealthMonitorIntervalMs   int
	HealthFailureThreshold    int
	RateLimitCooldownSeconds  int
	RestartBackoffBase        int
	RestartBackoffMax         int
	RestartBudgetLimit        int
	RestartBudgetWindow       int
	RestartQuarantineSeconds  int
	RestartBackoffJitter      int
	DegradedRetryAfter        int
	GatewayRLWindowSeconds    int
	GatewayRLFetchLimit       int
	GatewayRLDownloadLimit    int
	DrainTimeoutSeconds       int
	DrainPollIntervalMs       int
	RestartStabilizeSeconds   int
	UnhealthyRestartThreshold int
	RedisURL                  string
	WorkerCountries           map[string]string
}

func LoadConfig() Config {
	youtubeFetchWorker := strings.ToLower(strings.TrimSpace(getenvDefault("YOUTUBE_FETCH_WORKER", "")))
	youtubeFetchWorkers := parseWorkerList(getenvDefault("YOUTUBE_FETCH_WORKERS", ""))
	if len(youtubeFetchWorkers) == 0 && youtubeFetchWorker != "" {
		youtubeFetchWorkers = []string{youtubeFetchWorker}
	}

	workerCountries := parseWorkerCountries(getenvDefault("WORKER_COUNTRIES", ""))

	uniqueCountries := make(map[string]struct{})
	for _, country := range workerCountries {
		c := strings.ToLower(strings.TrimSpace(country))
		if c != "" {
			uniqueCountries[c] = struct{}{}
		}
	}
	numCountries := len(uniqueCountries)
	maxRetries := envInt("MAX_RETRIES", 3)
	if numCountries > maxRetries {
		maxRetries = numCountries
	}

	return Config{
		GatewayPort:               envInt("GATEWAY_PORT", 9111),
		WorkerCount:               envInt("WORKER_COUNT", 3),
		ExtractorIPCEnabled:       envBool("EXTRACTOR_IPC_ENABLED", false),
		YouTubeFetchIPv6Only:      envBool("YOUTUBE_FETCH_IPV6_ONLY", false),
		YouTubeFetchWorker:        youtubeFetchWorker,
		YouTubeFetchWorkers:       youtubeFetchWorkers,
		ExtractorPythonBin:        getenvDefault("EXTRACTOR_PYTHON_BIN", "python3"),
		ExtractorWorkerPath:       getenvDefault("EXTRACTOR_WORKER_PATH", "../extractor/worker_daemon.py"),
		ExtractorTimeoutMs:        envInt("EXTRACTOR_TIMEOUT_MS", 45000),
		DegradedTimeoutSeconds:    envInt("DEGRADED_TIMEOUT_SECONDS", 30),
		GluetunPassword:           getenvDefault("GLUETUN_PASSWORD", "secretpassword"),
		MaxRetries:                maxRetries,
		TikTokAPIKey:              getenvDefault("TIKTOK_API_KEY", ""),
		HealthCheckTimeoutMs:      envInt("HEALTH_CHECK_TIMEOUT_MS", 8000),
		HealthMonitorIntervalMs:   envInt("HEALTH_MONITOR_INTERVAL_MS", 5000),
		HealthFailureThreshold:    envInt("HEALTH_FAILURE_THRESHOLD", 3),
		RateLimitCooldownSeconds:  envInt("RATE_LIMIT_COOLDOWN", 300),
		RestartBackoffBase:        envInt("RESTART_BACKOFF_BASE", 30),
		RestartBackoffMax:         envInt("RESTART_BACKOFF_MAX", 300),
		RestartBudgetLimit:        envInt("RESTART_BUDGET_LIMIT", 3),
		RestartBudgetWindow:       envInt("RESTART_BUDGET_WINDOW", 600),
		RestartQuarantineSeconds:  envInt("RESTART_QUARANTINE_SECONDS", 600),
		RestartBackoffJitter:      envInt("RESTART_BACKOFF_JITTER", 5),
		DegradedRetryAfter:        envInt("DEGRADED_RETRY_AFTER", 5),
		GatewayRLWindowSeconds:    envInt("GATEWAY_RL_WINDOW_SECONDS", 60),
		GatewayRLFetchLimit:       envInt("GATEWAY_RL_FETCH_LIMIT", 45),
		GatewayRLDownloadLimit:    envInt("GATEWAY_RL_DOWNLOAD_LIMIT", 45),
		DrainTimeoutSeconds:       envInt("DRAIN_TIMEOUT_SECONDS", 90),
		DrainPollIntervalMs:       envInt("DRAIN_POLL_INTERVAL_MS", 500),
		RestartStabilizeSeconds:   envInt("RESTART_STABILIZE_SECONDS", 2),
		UnhealthyRestartThreshold: envInt("UNHEALTHY_RESTART_THRESHOLD", 6),
		RedisURL:                  getenvDefault("REDIS_URL", "redis://172.30.0.250:6379/0"),
		WorkerCountries:           workerCountries,
	}
}

func envInt(key string, fallback int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func envBool(key string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func getenvDefault(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func parseWorkerList(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	seen := make(map[string]struct{}, len(parts))
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		w := strings.ToLower(strings.TrimSpace(part))
		if w == "" {
			continue
		}
		if _, ok := seen[w]; ok {
			continue
		}
		seen[w] = struct{}{}
		out = append(out, w)
	}
	return out
}

func ParseBoolQuery(raw string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(raw))
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func ExtractWorkerID(key string, workerCount int) string {
	if key == "" {
		return ""
	}
	var prefix string
	if idx := strings.Index(key, "::"); idx > 0 {
		prefix = key[:idx]
	} else if idx := strings.Index(key, "-"); idx > 0 {
		prefix = key[:idx]
	} else {
		return ""
	}
	for i := 1; i <= workerCount; i++ {
		if prefix == "w"+strconv.Itoa(i) {
			return prefix
		}
	}
	return ""
}

func parseWorkerCountries(raw string) map[string]string {
	// Initialize with defaults matching docker-compose.proton.yml (up to 18 workers)
	out := map[string]string{
		"w1": "Indonesia", "w2": "Indonesia", "w3": "Indonesia",
		"w4": "Singapore", "w5": "Singapore",
		"w6": "Vietnam", "w7": "Vietnam", "w8": "Vietnam", "w9": "Vietnam",
		"w10": "WARP",
		"w11": "Singapore", "w12": "Singapore", "w13": "Singapore",
		"w14": "Indonesia",
		"w15": "Singapore", "w16": "Singapore", "w17": "Singapore", "w18": "Singapore",
	}

	if strings.TrimSpace(raw) == "" {
		return out
	}

	parts := strings.Split(raw, ",")
	for _, part := range parts {
		kv := strings.Split(strings.TrimSpace(part), ":")
		if len(kv) == 2 {
			k := strings.ToLower(strings.TrimSpace(kv[0]))
			v := strings.TrimSpace(kv[1])
			if k != "" && v != "" {
				out[k] = v
			}
		}
	}
	return out
}
