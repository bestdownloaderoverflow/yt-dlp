package registry

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os/exec"
	"strings"
	"time"
)

type RotatorConfig struct {
	HealthCheckTimeoutMs    int
	GluetunPassword         string
	DrainTimeoutSeconds     int
	DrainPollIntervalMs     int
	RestartStabilizeSeconds int
}

type VPNRotator struct {
	registry *WorkerRegistry
	cfg      RotatorConfig
	healthCl *http.Client
}

func NewVPNRotator(registry *WorkerRegistry, cfg RotatorConfig) *VPNRotator {
	timeout := cfg.HealthCheckTimeoutMs
	if timeout < 1000 {
		timeout = 1000
	}
	return &VPNRotator{
		registry: registry,
		cfg:      cfg,
		healthCl: &http.Client{Timeout: time.Duration(timeout) * time.Millisecond},
	}
}

func (v *VPNRotator) HealthCheck(workerID string) bool {
	worker := v.registry.GetWorker(workerID)
	if worker == nil {
		return false
	}

	ipURL := fmt.Sprintf("%s/v1/publicip/ip", worker.ControlURL(v.cfg.GluetunPassword))
	if req, err := http.NewRequest(http.MethodGet, ipURL, nil); err == nil {
		resp, err := v.healthCl.Do(req)
		if err == nil {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 64))
			_ = resp.Body.Close()
			if ip := strings.TrimSpace(string(body)); ip != "" {
				log.Printf("[%s] VPN public IP: %s", workerID, ip)
			}
		}
	}

	addr := fmt.Sprintf("%s:%d", worker.Host, worker.APIPort)
	timeout := v.healthCl.Timeout
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		log.Printf("[%s] worker TCP health check error: %v", workerID, err)
		return false
	}
	_ = conn.Close()
	return true
}

func (v *VPNRotator) WaitForDrain(ctx context.Context, workerID string) bool {
	timeout := time.Duration(v.cfg.DrainTimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	interval := time.Duration(v.cfg.DrainPollIntervalMs) * time.Millisecond
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	logInterval := 5 * time.Second
	deadline := time.Now().Add(timeout)
	lastLoggedActive := -1
	lastLogAt := time.Time{}
	for {
		if v.registry.IsWorkerIdle(workerID) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(interval):
			active := v.registry.ActiveRequests(workerID)
			now := time.Now()
			if active != lastLoggedActive || lastLogAt.IsZero() || now.Sub(lastLogAt) >= logInterval {
				log.Printf("[%s] drain wait: active_requests=%d", workerID, active)
				lastLoggedActive = active
				lastLogAt = now
			}
		}
	}
}

func (v *VPNRotator) RestartWorker(ctx context.Context, workerID string) bool {
	drained := v.WaitForDrain(ctx, workerID)
	if !drained {
		log.Printf("[%s] drain timeout reached; forcing container restart", workerID)
	}

	v.registry.UpdateRestartState(workerID, true)
	gluetun := fmt.Sprintf("ytdlp-gluetun-%s", strings.TrimPrefix(workerID, "w"))
	ytdlp := fmt.Sprintf("ytdlp-stream-%s", strings.TrimPrefix(workerID, "w"))

	log.Printf("[%s] restarting %s", workerID, gluetun)
	if err := v.restartContainer(gluetun); err != nil {
		log.Printf("[%s] restart error: %v", workerID, err)
		v.registry.MarkRestarted(workerID, false)
		return false
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(10 * time.Second):
	}

	log.Printf("[%s] restarting %s", workerID, ytdlp)
	if err := v.restartContainer(ytdlp); err != nil {
		log.Printf("[%s] restart error: %v", workerID, err)
		v.registry.MarkRestarted(workerID, false)
		return false
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(5 * time.Second):
	}

	healthy := false
	for i := 0; i < 6; i++ {
		if v.HealthCheck(workerID) {
			healthy = true
			break
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(5 * time.Second):
		}
	}

	if healthy {
		stabilize := time.Duration(v.cfg.RestartStabilizeSeconds) * time.Second
		if stabilize < 0 {
			stabilize = 0
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(stabilize):
		}
		v.registry.MarkRestarted(workerID, true)
		return true
	}

	log.Printf("[%s] health check failed after restart", workerID)
	v.registry.MarkRestarted(workerID, false)
	return false
}

func (v *VPNRotator) restartContainer(container string) error {
	cmd := exec.Command("docker", "restart", "--time", "30", container)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker restart %s failed: %v (%s)", container, err, strings.TrimSpace(string(out)))
	}
	return nil
}
