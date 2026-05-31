package utils

import "testing"

func TestSlidingWindowRateLimiterRecordsFirstEvent(t *testing.T) {
	limiter := NewSlidingWindowRateLimiter(1, 60)

	allowed, retryAfter := limiter.Check("client-1")
	if !allowed {
		t.Fatalf("first request should be allowed, retry_after=%d", retryAfter)
	}

	allowed, retryAfter = limiter.Check("client-1")
	if allowed {
		t.Fatal("second request in the same window should be rate limited")
	}
	if retryAfter < 1 {
		t.Fatalf("retry_after should be positive, got %d", retryAfter)
	}
}
