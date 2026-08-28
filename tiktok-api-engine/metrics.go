package main

import (
	"fmt"
	"sort"
	"strings"
	"sync"
)

// Metrics is a small Prometheus counter store. It is process-local on purpose:
// this service runs as one binary, so there is nothing to share counters with.
type Metrics struct {
	mu       sync.Mutex
	counters map[string]int64
	labels   map[string][]string
	names    map[string]string
}

func NewMetrics() *Metrics {
	return &Metrics{
		counters: map[string]int64{},
		labels:   map[string][]string{},
		names:    map[string]string{},
	}
}

func (m *Metrics) Inc(name string, labelPairs ...string) {
	if len(labelPairs)%2 != 0 {
		labelPairs = labelPairs[:len(labelPairs)-1]
	}
	key := name
	if len(labelPairs) > 0 {
		key += "|" + strings.Join(labelPairs, "\x00")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.counters[key]++
	m.labels[key] = labelPairs
	m.names[key] = name
}

func escapeLabel(v string) string {
	v = strings.ReplaceAll(v, `\`, `\\`)
	v = strings.ReplaceAll(v, `"`, `\"`)
	return strings.ReplaceAll(v, "\n", `\n`)
}

func (m *Metrics) Render(uptimeSeconds int64) string {
	m.mu.Lock()
	keys := make([]string, 0, len(m.counters))
	for k := range m.counters {
		keys = append(keys, k)
	}
	snapshot := make(map[string]int64, len(m.counters))
	for k, v := range m.counters {
		snapshot[k] = v
	}
	labels := make(map[string][]string, len(m.labels))
	for k, v := range m.labels {
		labels[k] = v
	}
	names := make(map[string]string, len(m.names))
	for k, v := range m.names {
		names[k] = v
	}
	m.mu.Unlock()
	sort.Strings(keys)

	var b strings.Builder
	b.WriteString("# HELP tiktok_api_uptime_seconds Uptime in seconds\n")
	b.WriteString("# TYPE tiktok_api_uptime_seconds gauge\n")
	fmt.Fprintf(&b, "tiktok_api_uptime_seconds %d\n", uptimeSeconds)

	seen := map[string]bool{}
	for _, key := range keys {
		name := names[key]
		if !seen[name] {
			fmt.Fprintf(&b, "# HELP %s Total %s\n# TYPE %s counter\n", name, name, name)
			seen[name] = true
		}
		pairs := labels[key]
		if len(pairs) == 0 {
			fmt.Fprintf(&b, "%s %d\n", name, snapshot[key])
			continue
		}
		var parts []string
		for i := 0; i+1 < len(pairs); i += 2 {
			parts = append(parts, fmt.Sprintf("%s=%q", pairs[i], escapeLabel(pairs[i+1])))
		}
		fmt.Fprintf(&b, "%s{%s} %d\n", name, strings.Join(parts, ","), snapshot[key])
	}
	return b.String()
}
