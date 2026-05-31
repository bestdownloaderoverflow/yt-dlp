package utils

import (
	"sync"
	"time"
)

type SlidingWindowRateLimiter struct {
	mu            sync.Mutex
	limit         int
	windowSeconds int
	events        map[string][]time.Time
}

func NewSlidingWindowRateLimiter(limit, windowSeconds int) *SlidingWindowRateLimiter {
	if limit < 1 {
		limit = 1
	}
	if windowSeconds < 1 {
		windowSeconds = 1
	}
	return &SlidingWindowRateLimiter{limit: limit, windowSeconds: windowSeconds, events: map[string][]time.Time{}}
}

func (l *SlidingWindowRateLimiter) Check(key string) (bool, int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	cutoff := now.Add(-time.Duration(l.windowSeconds) * time.Second)
	q := l.events[key]
	idx := 0
	for idx < len(q) && (q[idx].Before(cutoff) || q[idx].Equal(cutoff)) {
		idx++
	}
	if idx > 0 {
		q = q[idx:]
	}
	if len(q) == 0 {
		l.events[key] = []time.Time{now}
		return true, 0
	}
	if len(q) >= l.limit {
		retryAfter := int(q[0].Add(time.Duration(l.windowSeconds) * time.Second).Sub(now).Seconds())
		if retryAfter < 1 {
			retryAfter = 1
		}
		l.events[key] = q
		return false, retryAfter
	}
	q = append(q, now)
	l.events[key] = q
	return true, 0
}
