package metrics

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	WorkerRestartsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "worker_restarts_total",
			Help: "Total number of worker container restarts",
		},
		[]string{"worker_id", "success"},
	)

	WorkerFailuresTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "worker_failures_total",
			Help: "Total number of worker probe/health failures",
		},
		[]string{"worker_id", "type"},
	)

	WorkerHealthy = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "worker_healthy",
			Help: "Current health status of worker (1=healthy, 0=unhealthy)",
		},
		[]string{"worker_id"},
	)

	WorkerActiveRequests = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "worker_active_requests",
			Help: "Current number of active requests per worker",
		},
		[]string{"worker_id"},
	)

	WorkerRestartDuration = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "worker_restart_duration_seconds",
			Help:    "Duration of worker restart operations in seconds",
			Buckets: prometheus.ExponentialBuckets(1, 2, 8), // 1, 2, 4, 8, 16, 32, 64, 128 seconds
		},
		[]string{"worker_id", "success"},
	)

	WorkerRestartReasonsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "worker_restart_reasons_total",
			Help: "Total number of worker restart attempts by reason",
		},
		[]string{"worker_id", "reason_code", "success"},
	)

	GatewayWorkersTotal = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "gateway_workers_total",
			Help: "Total number of configured workers",
		},
	)

	GatewayHealthyWorkers = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "gateway_healthy_workers",
			Help: "Number of currently healthy workers",
		},
	)
)

// Register all metrics with the Prometheus client registry.
func Register() {
	prometheus.MustRegister(
		WorkerRestartsTotal,
		WorkerFailuresTotal,
		WorkerHealthy,
		WorkerActiveRequests,
		WorkerRestartDuration,
		WorkerRestartReasonsTotal,
		GatewayWorkersTotal,
		GatewayHealthyWorkers,
	)
}

// IncRestart increments the worker_restarts_total counter.
func IncRestart(workerID string, success bool) {
	WorkerRestartsTotal.WithLabelValues(workerID, boolToString(success)).Inc()
}

// IncFailure increments the worker_failures_total counter.
func IncFailure(workerID string, failureType string) {
	WorkerFailuresTotal.WithLabelValues(workerID, failureType).Inc()
}

// SetHealth sets the worker_healthy gauge.
func SetHealth(workerID string, healthy bool) {
	WorkerHealthy.WithLabelValues(workerID).Set(boolToFloat(healthy))
}

// SetActiveRequests sets the worker_active_requests gauge.
func SetActiveRequests(workerID string, count int) {
	WorkerActiveRequests.WithLabelValues(workerID).Set(float64(count))
}

// ObserveRestartDuration observes the restart duration histogram.
func ObserveRestartDuration(workerID string, success bool, durationSeconds float64) {
	WorkerRestartDuration.WithLabelValues(workerID, boolToString(success)).Observe(durationSeconds)
}

// IncRestartReason increments the worker restart reason counter.
func IncRestartReason(workerID string, reasonCode string, success bool) {
	WorkerRestartReasonsTotal.WithLabelValues(workerID, reasonCode, boolToString(success)).Inc()
}

// SetGatewayWorkers sets the gateway_workers_total gauge.
func SetGatewayWorkers(total int) {
	GatewayWorkersTotal.Set(float64(total))
}

// SetGatewayHealthyWorkers sets the gateway_healthy_workers gauge.
func SetGatewayHealthyWorkers(count int) {
	GatewayHealthyWorkers.Set(float64(count))
}

func boolToString(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

func boolToFloat(b bool) float64 {
	if b {
		return 1.0
	}
	return 0.0
}

// ServeHTTP handles Prometheus metrics requests.
func ServeHTTP(w http.ResponseWriter, r *http.Request) {
	promhttp.Handler().ServeHTTP(w, r)
}
