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
	ExtractorPythonBin        string
	ExtractorWorkerPath       string
	ExtractorTimeoutMs        int
	DegradedTimeoutSeconds    int
	GluetunPassword           string
	MaxRetries                int
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
}

func LoadConfig() Config {
	return Config{
		GatewayPort:               envInt("GATEWAY_PORT", 9111),
		WorkerCount:               envInt("WORKER_COUNT", 3),
		ExtractorIPCEnabled:       envBool("EXTRACTOR_IPC_ENABLED", false),
		ExtractorPythonBin:        getenvDefault("EXTRACTOR_PYTHON_BIN", "python3"),
		ExtractorWorkerPath:       getenvDefault("EXTRACTOR_WORKER_PATH", "../extractor/worker_daemon.py"),
		ExtractorTimeoutMs:        envInt("EXTRACTOR_TIMEOUT_MS", 45000),
		DegradedTimeoutSeconds:    envInt("DEGRADED_TIMEOUT_SECONDS", 30),
		GluetunPassword:           getenvDefault("GLUETUN_PASSWORD", "secretpassword"),
		MaxRetries:                envInt("MAX_RETRIES", 3),
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
