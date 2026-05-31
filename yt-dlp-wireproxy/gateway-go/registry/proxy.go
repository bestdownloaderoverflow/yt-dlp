package registry

import (
	"fmt"
	"log"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"

	"gateway-go/metrics"
)

type Proxy struct {
	ID               string
	Index            int
	Country          string
	Container        string
	SOCKS5URL        string
	Healthy          bool
	Restarting       bool
	Cooldown         bool
	CooldownUntil    time.Time
	BackoffUntil     time.Time
	QuarantineUntil  time.Time
	LastExitIP       string
	PreviousExitIP   string
	Failures         int
	ProbeFailures    int
	RestartFailures  int
	RestartEvents    []time.Time
	ActiveRequests   int64
	RestartScheduled bool
	StartedAt        time.Time
}

func (p *Proxy) IsAccepting() bool {
	now := time.Now()
	if !p.Healthy {
		return false
	}
	if p.Restarting || p.RestartScheduled {
		return false
	}
	if p.Cooldown && now.Before(p.CooldownUntil) {
		return false
	}
	if !p.BackoffUntil.IsZero() && now.Before(p.BackoffUntil) {
		return false
	}
	if !p.QuarantineUntil.IsZero() && now.Before(p.QuarantineUntil) {
		return false
	}
	return true
}

func (p *Proxy) ActiveCount() int {
	v := p.ActiveRequests
	if v < 0 {
		return 0
	}
	if v > int64(^uint(0)>>1) {
		return int(^uint(0) >> 1)
	}
	return int(v)
}

type ProxyConfig struct {
	ProxyCount               int
	ProxyCooldownSeconds     int
	ProxyCountriess          map[int]string
	HealthFailureThreshold   int
	RestartBackoffBase       int
	RestartBackoffMax        int
	RestartBackoffJitter     int
	RestartQuarantineSeconds int
	RestartBudgetLimit       int
	RestartBudgetWindow      int
}

type ProxyRegistry struct {
	mu        sync.Mutex
	proxies   []*Proxy
	cfg       ProxyConfig
	countryRR map[string]*atomic.Uint64
}

func NewProxyRegistry(cfg ProxyConfig) *ProxyRegistry {
	now := time.Now()
	proxies := make([]*Proxy, 0, cfg.ProxyCount)
	countryRR := make(map[string]*atomic.Uint64)

	for i := 1; i <= cfg.ProxyCount; i++ {
		container := fmt.Sprintf("wireproxy-%02d", i)
		socksURL := fmt.Sprintf("socks5h://%s:1080", container)
		country := cfg.ProxyCountriess[i]
		if country == "" {
			country = "unknown"
		}
		proxies = append(proxies, &Proxy{
			ID:        fmt.Sprintf("p%d", i),
			Index:     i,
			Country:   country,
			Container: container,
			SOCKS5URL: socksURL,
			Healthy:   false,
			StartedAt: now,
		})
		if _, ok := countryRR[country]; !ok {
			countryRR[country] = &atomic.Uint64{}
		}
		metrics.SetProxyHealth(i, country, false)
	}
	log.Printf("initialized %d proxy entries across %d countries", cfg.ProxyCount, len(countryRR))
	return &ProxyRegistry{proxies: proxies, cfg: cfg, countryRR: countryRR}
}

func (r *ProxyRegistry) getProxyUnlocked(proxyID string) *Proxy {
	for _, p := range r.proxies {
		if p.ID == proxyID {
			return p
		}
	}
	return nil
}

func (r *ProxyRegistry) getProxyByIndex(idx int) *Proxy {
	if idx < 1 || idx > len(r.proxies) {
		return nil
	}
	return r.proxies[idx-1]
}

func (r *ProxyRegistry) GetProxy(proxyID string) *Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return nil
	}
	copyP := *p
	return &copyP
}

func (r *ProxyRegistry) SelectProxy(exclude map[string]bool, excludeCountries map[string]bool) *Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	var candidates []*Proxy

	for _, p := range r.proxies {
		if !p.Healthy {
			continue
		}
		if p.Restarting || p.RestartScheduled {
			continue
		}
		if p.Cooldown && now.Before(p.CooldownUntil) {
			continue
		}
		if !p.BackoffUntil.IsZero() && now.Before(p.BackoffUntil) {
			continue
		}
		if !p.QuarantineUntil.IsZero() && now.Before(p.QuarantineUntil) {
			continue
		}
		if exclude != nil && exclude[p.ID] {
			continue
		}
		if excludeCountries != nil && p.Country != "" && excludeCountries[p.Country] {
			continue
		}
		candidates = append(candidates, p)
	}

	if len(candidates) == 0 {
		return nil
	}

	bestLoad := int(^uint(0) >> 1)
	best := make([]*Proxy, 0, len(candidates))
	for _, p := range candidates {
		load := p.ActiveCount()
		if load < bestLoad {
			bestLoad = load
			best = []*Proxy{p}
		} else if load == bestLoad {
			best = append(best, p)
		}
	}

	if len(best) == 1 {
		return best[0]
	}

	selected := best[rand.Intn(len(best))]
	copyP := *selected
	return &copyP
}

func (r *ProxyRegistry) SelectProxyWithCountrySpread(exclude map[string]bool, excludeCountries map[string]bool) *Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	type candidate struct {
		proxy   *Proxy
		country string
	}

	var candidates []candidate

	for _, p := range r.proxies {
		if !p.Healthy {
			continue
		}
		if p.Restarting || p.RestartScheduled {
			continue
		}
		if p.Cooldown && now.Before(p.CooldownUntil) {
			continue
		}
		if !p.BackoffUntil.IsZero() && now.Before(p.BackoffUntil) {
			continue
		}
		if !p.QuarantineUntil.IsZero() && now.Before(p.QuarantineUntil) {
			continue
		}
		if exclude != nil && exclude[p.ID] {
			continue
		}
		if excludeCountries != nil && p.Country != "" && excludeCountries[p.Country] {
			continue
		}
		candidates = append(candidates, candidate{proxy: p, country: p.Country})
	}

	if len(candidates) == 0 {
		return nil
	}

	bestLoad := int(^uint(0) >> 1)
	best := make([]*Proxy, 0, len(candidates))
	for _, c := range candidates {
		load := c.proxy.ActiveCount()
		if load < bestLoad {
			bestLoad = load
			best = []*Proxy{c.proxy}
		} else if load == bestLoad {
			best = append(best, c.proxy)
		}
	}

	if len(best) == 1 {
		copyP := *best[0]
		return &copyP
	}

	selected := best[rand.Intn(len(best))]
	copyP := *selected
	return &copyP
}

func (r *ProxyRegistry) MarkCooldown(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.Cooldown = true
		p.CooldownUntil = time.Now().Add(time.Duration(r.cfg.ProxyCooldownSeconds) * time.Second)
		p.Failures++
		log.Printf("[proxy:%s] marked cooldown for %ds", proxyID, r.cfg.ProxyCooldownSeconds)
		metrics.IncProxyFailure(proxyID, "cooldown")
	}
}

func (r *ProxyRegistry) MarkRateLimited(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.Cooldown = true
		p.CooldownUntil = time.Now().Add(time.Duration(r.cfg.ProxyCooldownSeconds) * time.Second)
		p.Failures++
		log.Printf("[proxy:%s] marked rate limited; cooldown %ds", proxyID, r.cfg.ProxyCooldownSeconds)
		metrics.IncProxyFailure(proxyID, "rate_limit")
	}
}

func (r *ProxyRegistry) SetHealthy(proxyID string, healthy bool, exitIP string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		if p.Healthy != healthy {
			if healthy {
				log.Printf("[proxy:%s] marked healthy (exit_ip=%s)", proxyID, exitIP)
			} else {
				log.Printf("[proxy:%s] marked unhealthy", proxyID)
			}
		}
		p.Healthy = healthy
		if exitIP != "" {
			p.PreviousExitIP = p.LastExitIP
			p.LastExitIP = exitIP
		}
		if healthy {
			p.ProbeFailures = 0
			r.clearExpiredCooldownUnlocked(p, time.Now())
		}
		metrics.SetProxyHealth(p.Index, p.Country, healthy)
	}
}

func (r *ProxyRegistry) SetExitIP(proxyID string, ip string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		if p.LastExitIP != "" {
			p.PreviousExitIP = p.LastExitIP
		}
		p.LastExitIP = ip
	}
}

func (r *ProxyRegistry) RecordProbe(proxyID string, healthy bool, exitIP string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return
	}

	threshold := r.cfg.HealthFailureThreshold
	if threshold < 1 {
		threshold = 1
	}

	if healthy {
		p.ProbeFailures = 0
		if exitIP != "" {
			p.PreviousExitIP = p.LastExitIP
			p.LastExitIP = exitIP
		}
		if !p.Healthy {
			log.Printf("[proxy:%s] marked healthy after successful probe (exit_ip=%s)", proxyID, exitIP)
		}
		p.Healthy = true
		r.clearExpiredCooldownUnlocked(p, time.Now())
		metrics.SetProxyHealth(p.Index, p.Country, true)
		return
	}

	p.ProbeFailures++
	if p.ProbeFailures >= threshold {
		if p.Healthy {
			log.Printf("[proxy:%s] marked unhealthy after %d consecutive probe failures", proxyID, p.ProbeFailures)
		}
		p.Healthy = false
		metrics.IncProxyFailure(proxyID, "probe")
		metrics.SetProxyHealth(p.Index, p.Country, false)
	}
}

func (r *ProxyRegistry) IncrementActive(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		atomic.AddInt64(&p.ActiveRequests, 1)
	}
}

func (r *ProxyRegistry) DecrementActive(proxyID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		v := atomic.AddInt64(&p.ActiveRequests, -1)
		if v < 0 {
			atomic.StoreInt64(&p.ActiveRequests, 0)
		}
	}
}

func (r *ProxyRegistry) IsProxyIdle(proxyID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		return p.ActiveCount() == 0
	}
	return true
}

func (r *ProxyRegistry) ActiveRequests(proxyID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		return p.ActiveCount()
	}
	return 0
}

func (r *ProxyRegistry) ShouldRestartUnhealthy(proxyID string, threshold int) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return false
	}
	if threshold < 1 {
		return false
	}
	if p.Healthy || p.Restarting || p.RestartScheduled {
		return false
	}
	if p.ActiveCount() > 0 {
		return false
	}
	return p.ProbeFailures >= threshold
}

func (r *ProxyRegistry) ScheduleRestart(proxyID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		now := time.Now()
		if now.Before(p.BackoffUntil) || now.Before(p.QuarantineUntil) {
			return false
		}
		if !p.Restarting && !p.RestartScheduled {
			p.RestartScheduled = true
			log.Printf("[proxy:%s] restart scheduled", proxyID)
			return true
		}
	}
	return false
}

func (r *ProxyRegistry) UpdateRestartState(proxyID string, restarting bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if p := r.getProxyUnlocked(proxyID); p != nil {
		p.Restarting = restarting
		if restarting {
			p.Healthy = false
		}
	}
}

func (r *ProxyRegistry) MarkRestarted(proxyID string, success bool, exitIP string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.getProxyUnlocked(proxyID)
	if p == nil {
		return
	}

	p.Restarting = false
	p.RestartScheduled = false

	if success {
		p.Healthy = true
		p.Failures = 0
		p.RestartFailures = 0
		p.ProbeFailures = 0
		p.Cooldown = false
		p.CooldownUntil = time.Time{}
		p.BackoffUntil = time.Time{}
		p.QuarantineUntil = time.Time{}
		p.RestartEvents = nil
		p.StartedAt = time.Now()
		if exitIP != "" {
			p.PreviousExitIP = p.LastExitIP
			p.LastExitIP = exitIP
		}
		log.Printf("[proxy:%s] restarted successfully (exit_ip=%s)", proxyID, exitIP)
		metrics.IncProxyRestart(proxyID, true)
		metrics.SetProxyHealth(p.Index, p.Country, true)
		return
	}

	now := time.Now()
	p.Healthy = false
	p.Failures++
	p.RestartFailures++

	exp := p.RestartFailures - 1
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
	p.BackoffUntil = now.Add(time.Duration(delay) * time.Second)

	windowStart := now.Add(-time.Duration(r.cfg.RestartBudgetWindow) * time.Second)
	filtered := make([]time.Time, 0, len(p.RestartEvents))
	for _, event := range p.RestartEvents {
		if event.After(windowStart) {
			filtered = append(filtered, event)
		}
	}
	p.RestartEvents = append(filtered, now)
	if r.cfg.RestartBudgetLimit > 0 && len(p.RestartEvents) >= r.cfg.RestartBudgetLimit {
		p.QuarantineUntil = now.Add(time.Duration(r.cfg.RestartQuarantineSeconds) * time.Second)
		p.BackoffUntil = p.QuarantineUntil
		log.Printf("[proxy:%s] entering quarantine for %ds after %d restart failures", proxyID, r.cfg.RestartQuarantineSeconds, len(p.RestartEvents))
	}
	p.RestartScheduled = true

	log.Printf("[proxy:%s] restart failed; backoff %ds (restart_failures=%d)", proxyID, delay, p.RestartFailures)
	metrics.IncProxyRestart(proxyID, false)
	metrics.SetProxyHealth(p.Index, p.Country, false)
}

func (r *ProxyRegistry) ProxiesSnapshot() []Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Proxy, 0, len(r.proxies))
	for _, p := range r.proxies {
		out = append(out, *p)
	}
	return out
}

func (r *ProxyRegistry) ProxiesNeedRestart() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var needing []string
	for _, p := range r.proxies {
		now := time.Now()
		if p.RestartScheduled && !p.Restarting && !now.Before(p.BackoffUntil) && !now.Before(p.QuarantineUntil) {
			needing = append(needing, p.ID)
		}
	}
	return needing
}

func (r *ProxyRegistry) clearExpiredCooldownUnlocked(p *Proxy, now time.Time) {
	if p.Cooldown && !now.Before(p.CooldownUntil) {
		p.Cooldown = false
		p.CooldownUntil = time.Time{}
	}
}

func (r *ProxyRegistry) GetProxyByURL(socksURL string) *Proxy {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, p := range r.proxies {
		if p.SOCKS5URL == socksURL {
			copyP := *p
			return &copyP
		}
	}
	return nil
}
