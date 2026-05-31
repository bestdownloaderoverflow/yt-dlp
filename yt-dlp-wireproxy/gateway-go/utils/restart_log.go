package utils

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"time"

	"github.com/redis/go-redis/v9"
)

// RestartLogEntry represents a single worker restart/failure event persisted to Redis.
type RestartLogEntry struct {
	Timestamp       int64  `json:"timestamp"` // Unix millis
	WorkerID        string `json:"worker_id"`
	Success         bool   `json:"success"`
	DurationMs      int64  `json:"duration_ms"`    // -1 if not applicable (e.g., failed before restart started)
	ProbeFailures   int    `json:"probe_failures"` // probe failures count at restart time
	RestartFailures int    `json:"restart_failures"`
	Quarantine      bool   `json:"quarantine"`  // was quarantine triggered prior to this restart
	ReasonCode      string `json:"reason_code"` // probe_failure, rate_limit, uptime, health_monitor, unknown
}

var redisClient *redis.Client

// InitRedis initializes the Redis client for restart logging.
// Returns nil if successful, or an error if Redis is unreachable.
func InitRedis(redisURL string) error {
	// Parse URL to extract host:port. Accept both "redis://host:port" and "host:port"
	addr := redisURL
	if u, err := url.Parse(redisURL); err == nil && u.Host != "" {
		addr = u.Host
	}
	opts := &redis.Options{
		Addr: addr,
	}
	// Override pool size if needed — keep small since we only do occasional LPUSH
	client := redis.NewClient(opts)
	ctx := context.Background()
	if err := client.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("redis ping failed: %w", err)
	}
	redisClient = client
	return nil
}

// LogRestart records a restart event to Redis.
// If Redis is unavailable, the error is logged but not returned (fire-and-forget semantics).
func LogRestart(entry RestartLogEntry) error {
	if redisClient == nil {
		// Redis not configured — silently skip
		return nil
	}
	entry.Timestamp = time.Now().UnixMilli()
	data, err := json.Marshal(entry)
	if err != nil {
		return fmt.Errorf("json marshal: %w", err)
	}
	key := fmt.Sprintf("restart_log:%s", entry.WorkerID)
	ctx := context.Background()
	pipe := redisClient.Pipeline()
	pipe.LPush(ctx, key, data)
	pipe.LTrim(ctx, key, 0, 999) // keep last 1000 entries
	pipe.Expire(ctx, key, 30*24*time.Hour)
	_, err = pipe.Exec(ctx)
	return err
}

// GetRestartHistory retrieves recent restart events for a worker.
// limit is the maximum number of entries to return (max 1000).
func GetRestartHistory(workerID string, limit int) ([]RestartLogEntry, error) {
	if redisClient == nil {
		return nil, fmt.Errorf("redis client not initialized")
	}
	if limit <= 0 || limit > 1000 {
		limit = 1000
	}
	key := fmt.Sprintf("restart_log:%s", workerID)
	ctx := context.Background()
	raw, err := redisClient.LRange(ctx, key, 0, int64(limit)-1).Result()
	if err != nil {
		return nil, fmt.Errorf("redis lrange: %w", err)
	}
	out := make([]RestartLogEntry, 0, len(raw))
	for _, v := range raw {
		var e RestartLogEntry
		if err := json.Unmarshal([]byte(v), &e); err == nil {
			out = append(out, e)
		}
	}
	return out, nil
}

// GetAllWorkersRestartHistory returns restart history for all configured workers.
func GetAllWorkersRestartHistory(workerIDs []string, limit int) (map[string][]RestartLogEntry, error) {
	result := make(map[string][]RestartLogEntry)
	for _, wid := range workerIDs {
		entries, err := GetRestartHistory(wid, limit)
		if err != nil {
			// Individual worker errors don't stop the whole operation
			continue
		}
		result[wid] = entries
	}
	return result, nil
}
