package registry

import (
	"testing"
	"time"
)

func newTestProxyRegistry() *ProxyRegistry {
	return NewProxyRegistry(ProxyConfig{
		ProxyCount:               1,
		ProxyCooldownSeconds:     300,
		ProxyCountriess:          map[int]string{1: "Indonesia"},
		HealthFailureThreshold:   1,
		RestartBackoffBase:       1,
		RestartBackoffMax:        1,
		RestartQuarantineSeconds: 60,
		RestartBudgetLimit:       2,
		RestartBudgetWindow:      600,
	})
}

func TestSuccessfulProbeDoesNotClearActiveCooldown(t *testing.T) {
	reg := newTestProxyRegistry()
	reg.SetHealthy("p1", true, "198.51.100.1")
	reg.MarkCooldown("p1")
	reg.RecordProbe("p1", true, "198.51.100.1")

	proxy := reg.GetProxy("p1")
	if proxy == nil {
		t.Fatal("expected p1")
	}
	if !proxy.Cooldown {
		t.Fatal("successful probe unexpectedly cleared active cooldown")
	}
	if proxy.IsAccepting() {
		t.Fatal("proxy in active cooldown must not accept traffic")
	}
}

func TestRestartBudgetQuarantinesRepeatedFailures(t *testing.T) {
	reg := newTestProxyRegistry()

	for attempt := 0; attempt < 2; attempt++ {
		if attempt == 0 && !reg.ScheduleRestart("p1") {
			t.Fatal("expected initial restart to be scheduled")
		}
		reg.mu.Lock()
		reg.proxies[0].BackoffUntil = time.Time{}
		reg.mu.Unlock()

		reg.UpdateRestartState("p1", true)
		reg.MarkRestarted("p1", false, "")
	}

	proxy := reg.GetProxy("p1")
	if proxy == nil {
		t.Fatal("expected p1")
	}
	if !time.Now().Before(proxy.QuarantineUntil) {
		t.Fatal("expected repeated restart failures to quarantine proxy")
	}
	if got := reg.ProxiesNeedRestart(); len(got) != 0 {
		t.Fatalf("quarantined proxy must wait before restart, got %v", got)
	}
}
