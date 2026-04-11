package registry

import (
	"fmt"
	"log"
	"math/rand"
	"sync"
	"time"

	"gateway-go/metrics"
)

const uptimeRestartInterval = 24 * time.Hour

type Worker struct {
	ID               string
	Host             string
	APIPort          int
	ProxyPort        int
	ControlPort      int
	Healthy          bool
	Restarting       bool
	RestartScheduled bool
	Failures         int
	RestartFailures  int
	ProbeFailures    int
	StartedAt        time.Time
	ActiveRequests   int
	LastRateLimit    time.Time
	DegradedUntil    time.Time
	NextRestartAt    time.Time
	QuarantineUntil  time.Time
	RestartEvents    []time.Time
	BreakerOpen      bool
}

func (w *Worker) APIURL() string {
	return fmt.Sprintf("http://%s:%d", w.Host, w.APIPort)
}

func (w *Worker) ControlURL(password string) string {
	return fmt.Sprintf("http://admin:%s@%s:%d", password, w.Host, w.ControlPort)
}

func (w *Worker) HTTPProxyURL() string {
	return fmt.Sprintf("http://%s:%d", w.Host, w.ProxyPort)
}

type Config struct {
	WorkerCount               int
	RateLimitCooldownSeconds  int
	DegradedTimeoutSeconds    int
	RestartBackoffBase        int
	RestartBackoffMax         int
	RestartBackoffJitter      int
	RestartBudgetLimit        int
	RestartBudgetWindow       int
	RestartQuarantineSeconds  int
	HealthFailureThreshold    int
	UnhealthyRestartThreshold int
}

type WorkerRegistry struct {
	mu      sync.Mutex
	workers []*Worker
	cfg     Config
}

func NewWorkerRegistry(cfg Config) *WorkerRegistry {
	now := time.Now()
	workers := make([]*Worker, 0, cfg.WorkerCount)
	for i := 1; i <= cfg.WorkerCount; i++ {
		workers = append(workers, &Worker{
			ID:          fmt.Sprintf("w%d", i),
			Host:        fmt.Sprintf("gluetun-%d", i),
			APIPort:     9487,
			ProxyPort:   8888,
			ControlPort: 8000,
			Healthy:     false,
			StartedAt:   now,
		})
	}
	log.Printf("initialized %d workers", cfg.WorkerCount)
	return &WorkerRegistry{workers: workers, cfg: cfg}
}

func (r *WorkerRegistry) getWorkerUnlocked(workerID string) *Worker {
	for _, w := range r.workers {
		if w.ID == workerID {
			return w
		}
	}
	return nil
}

func (r *WorkerRegistry) GetWorker(workerID string) *Worker {
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return nil
	}
	copyW := *w
	return &copyW
}

func (r *WorkerRegistry) GetHealthyWorkers(exclude map[string]bool) []*Worker {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	result := make([]*Worker, 0, len(r.workers))
	for _, w := range r.workers {
		if now.Before(w.QuarantineUntil) {
			continue
		}
		if now.Before(w.DegradedUntil) {
			continue
		}
		if !w.Healthy || w.Restarting || w.RestartScheduled || w.BreakerOpen {
			continue
		}
		if exclude != nil && exclude[w.ID] {
			continue
		}
		if !w.LastRateLimit.IsZero() && now.Sub(w.LastRateLimit) <= time.Duration(r.cfg.RateLimitCooldownSeconds)*time.Second {
			continue
		}
		copyW := *w
		result = append(result, &copyW)
	}
	return result
}

func (r *WorkerRegistry) MarkRateLimited(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.LastRateLimit = time.Now()
		w.Failures++
		log.Printf("[%s] marked as rate limited", workerID)
		metrics.IncFailure(workerID, "rate_limit")
	}
}

func (r *WorkerRegistry) MarkFailure(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.Failures++
		// Only log when the worker is not already known-bad to avoid
		// flooding logs with hundreds of duplicate "marked as failed"
		// lines during a restart cycle.
		if !w.Restarting && !w.RestartScheduled && !w.BreakerOpen {
			log.Printf("[%s] marked as failed (failures=%d)", workerID, w.Failures)
		}
		metrics.IncFailure(workerID, "probe")
	}
}

func (r *WorkerRegistry) ScheduleRestart(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		if !w.Restarting && !w.RestartScheduled {
			w.RestartScheduled = true
			w.BreakerOpen = true
			log.Printf("[%s] restart scheduled", workerID)
			return true
		}
	}
	return false
}

func (r *WorkerRegistry) CanStartRestart(workerID string) (bool, string, int) {
	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return false, "unknown_worker", 0
	}
	now := time.Now()
	if w.Restarting {
		return false, "already_restarting", 0
	}
	if now.Before(w.QuarantineUntil) {
		return false, "quarantine", int(time.Until(w.QuarantineUntil).Seconds())
	}
	if now.Before(w.NextRestartAt) {
		return false, "backoff", int(time.Until(w.NextRestartAt).Seconds())
	}
	return true, "ok", 0
}

func (r *WorkerRegistry) UpdateRestartState(workerID string, restarting bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.Restarting = restarting
		if restarting {
			w.Healthy = false
		}
	}
}

func (r *WorkerRegistry) MarkRestarted(workerID string, success bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return
	}

	w.Restarting = false
	w.RestartScheduled = false
	if success {
		w.Healthy = true
		w.Failures = 0
		w.RestartFailures = 0
		w.ProbeFailures = 0
		w.StartedAt = time.Now()
		w.LastRateLimit = time.Time{}
		w.DegradedUntil = time.Time{}
		w.NextRestartAt = time.Time{}
		w.QuarantineUntil = time.Time{}
		w.RestartEvents = nil
		w.BreakerOpen = false
		log.Printf("[%s] restarted successfully", workerID)
		metrics.IncRestart(workerID, true)
		metrics.SetHealth(workerID, true)
		return
	}

	now := time.Now()
	w.Healthy = false
	w.Failures++
	w.RestartFailures++

	exp := w.RestartFailures - 1
	if exp < 0 {
		exp = 0
	}
	if exp > 6 {
		exp = 6
	}
	delay := r.cfg.RestartBackoffBase * (1 << exp)
	if delay > r.cfg.RestartBackoffMax {
		delay = r.cfg.RestartBackoffMax
	}
	if r.cfg.RestartBackoffJitter > 0 {
		delay += rand.Intn(r.cfg.RestartBackoffJitter + 1)
	}
	w.NextRestartAt = now.Add(time.Duration(delay) * time.Second)

	windowStart := now.Add(-time.Duration(r.cfg.RestartBudgetWindow) * time.Second)
	filtered := make([]time.Time, 0, len(w.RestartEvents))
	for _, t := range w.RestartEvents {
		if t.After(windowStart) {
			filtered = append(filtered, t)
		}
	}
	w.RestartEvents = append(filtered, now)
	if len(w.RestartEvents) >= r.cfg.RestartBudgetLimit {
		w.QuarantineUntil = now.Add(time.Duration(r.cfg.RestartQuarantineSeconds) * time.Second)
		w.NextRestartAt = w.QuarantineUntil
		log.Printf("[%s] entering quarantine for %ds after %d restart failures", workerID, r.cfg.RestartQuarantineSeconds, len(w.RestartEvents))
	}

	w.RestartScheduled = true
	w.BreakerOpen = true
	log.Printf("[%s] restart failed; retry after %ds", workerID, delay)
	metrics.IncRestart(workerID, false)
	metrics.SetHealth(workerID, false)
}

func (r *WorkerRegistry) OpenCircuit(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		if !w.BreakerOpen {
			log.Printf("[%s] circuit opened (drain mode)", workerID)
		}
		w.BreakerOpen = true
	}
}

func (r *WorkerRegistry) CloseCircuit(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.BreakerOpen = false
	}
}

func (r *WorkerRegistry) SetHealthy(workerID string, healthy bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		if w.Healthy != healthy {
			if healthy {
				log.Printf("[%s] marked healthy after successful probe", workerID)
			} else {
				log.Printf("[%s] marked unhealthy after failed probe", workerID)
			}
		}
		w.Healthy = healthy
		if healthy {
			w.BreakerOpen = false
			w.DegradedUntil = time.Time{}
		}
		metrics.SetHealth(workerID, healthy)
	}
}

func (r *WorkerRegistry) RecordProbe(workerID string, healthy bool) {
	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return
	}

	threshold := r.cfg.HealthFailureThreshold
	if threshold < 1 {
		threshold = 1
	}

	if healthy {
		w.ProbeFailures = 0
		if !w.Healthy {
			log.Printf("[%s] marked healthy after successful probe", workerID)
		}
		w.Healthy = true
		w.BreakerOpen = false
		w.DegradedUntil = time.Time{}
		metrics.SetHealth(workerID, true)
		return
	}

	w.ProbeFailures++
	if w.ProbeFailures >= threshold {
		if w.Healthy {
			log.Printf("[%s] marked unhealthy after %d consecutive failed probes", workerID, w.ProbeFailures)
		}
		w.Healthy = false
		metrics.IncFailure(workerID, "probe")
		metrics.SetHealth(workerID, false)
	}
}

func (r *WorkerRegistry) GetWorkersNeedingRestart() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	needing := []string{}
	now := time.Now()
	for _, w := range r.workers {
		uptime := now.Sub(w.StartedAt)
		if uptime >= uptimeRestartInterval && w.Healthy && !w.Restarting && !w.RestartScheduled {
			needing = append(needing, w.ID)
		}
	}
	return needing
}

func (r *WorkerRegistry) IsWorkerIdle(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		return w.ActiveRequests == 0
	}
	return false
}

func (r *WorkerRegistry) ActiveRequests(workerID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		return w.ActiveRequests
	}
	return 0
}

func (r *WorkerRegistry) IncrementActive(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil {
		w.ActiveRequests++
		metrics.SetActiveRequests(workerID, w.ActiveRequests)
	}
}

func (r *WorkerRegistry) DecrementActive(workerID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if w := r.getWorkerUnlocked(workerID); w != nil && w.ActiveRequests > 0 {
		w.ActiveRequests--
		metrics.SetActiveRequests(workerID, w.ActiveRequests)
	}
}

func (r *WorkerRegistry) ShouldRestartUnhealthy(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.getWorkerUnlocked(workerID)
	if w == nil {
		return false
	}

	threshold := r.cfg.UnhealthyRestartThreshold
	if threshold < 1 {
		return false
	}
	if w.Healthy || w.Restarting || w.RestartScheduled {
		return false
	}
	if w.ActiveRequests > 0 {
		return false
	}

	now := time.Now()
	if now.Before(w.QuarantineUntil) || now.Before(w.NextRestartAt) {
		return false
	}

	return w.ProbeFailures >= threshold
}

func (r *WorkerRegistry) WorkersSnapshot() []Worker {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Worker, 0, len(r.workers))
	for _, w := range r.workers {
		out = append(out, *w)
	}
	// Update aggregate metrics
	metrics.SetGatewayWorkers(len(r.workers))
	healthyCount := 0
	for _, w := range r.workers {
		if w.Healthy && !w.Restarting && !w.RestartScheduled && !w.BreakerOpen {
			healthyCount++
		}
	}
	metrics.SetGatewayHealthyWorkers(healthyCount)
	return out
}
