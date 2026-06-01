package handlers

import "testing"

func TestTikTokRPCErrorProxyPenalty(t *testing.T) {
	tests := []struct {
		name       string
		err        *IPCError
		want       bool
		wantReason string
	}{
		{
			name:       "rate limited code penalizes proxy",
			err:        &IPCError{Code: "rate_limited", Message: "rate limited", Status: 429},
			want:       true,
			wantReason: restartReasonRateLimit,
		},
		{
			name:       "ip blocked code penalizes proxy",
			err:        &IPCError{Code: "ip_blocked", Message: "blocked by TikTok", Status: 403},
			want:       true,
			wantReason: "ip_blocked",
		},
		{
			name:       "captcha challenge penalizes proxy",
			err:        &IPCError{Code: "download_error", Message: "Please verify you are human", Status: 403},
			want:       true,
			wantReason: "challenge",
		},
		{
			name: "unexpected response retries without proxy cooldown",
			err:  &IPCError{Code: "download_error", Message: "unexpected response from webpage request", Status: 502},
		},
		{
			name: "temporary platform error retries without proxy cooldown",
			err:  &IPCError{Code: "download_error", Message: "temporarily unavailable, try again later", Status: 503},
		},
		{
			name: "internal error retries without proxy cooldown",
			err:  &IPCError{Code: "internal_error", Message: "worker error", Status: 500},
		},
		{
			name: "upstream DNS failure retries without proxy cooldown",
			err:  &IPCError{Code: "upstream_dns_failure", Message: "temporary DNS failure", Status: 502},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, reason := tiktokRPCErrorProxyPenalty(tt.err)
			if got != tt.want {
				t.Fatalf("penalty=%v, want %v", got, tt.want)
			}
			if reason != tt.wantReason {
				t.Fatalf("reason=%q, want %q", reason, tt.wantReason)
			}
		})
	}
}

func TestTikTokRetryableErrorsDoNotAlwaysPenalizeProxy(t *testing.T) {
	retryOnly := []*IPCError{
		{Code: "download_error", Message: "unexpected response from webpage request", Status: 502},
		{Code: "download_error", Message: "temporarily unavailable, try again later", Status: 503},
		{Code: "internal_error", Message: "worker error", Status: 500},
	}

	for _, err := range retryOnly {
		if !isRetryableTikTokRPCError(err) {
			t.Fatalf("%#v should remain retryable", err)
		}
		if penalize, _ := tiktokRPCErrorProxyPenalty(err); penalize {
			t.Fatalf("%#v should not penalize proxy", err)
		}
	}
}
