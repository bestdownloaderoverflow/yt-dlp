use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

/// Simple in-memory rate limiter using sliding window
#[derive(Clone)]
pub struct RateLimiter {
    requests: Arc<RwLock<HashMap<IpAddr, Vec<Instant>>>>,
    max_requests: usize,
    window: Duration,
}

impl RateLimiter {
    pub fn new(max_requests: usize, window_secs: u64) -> Self {
        Self {
            requests: Arc::new(RwLock::new(HashMap::new())),
            max_requests,
            window: Duration::from_secs(window_secs),
        }
    }

    /// Check if request is allowed for given IP
    /// Returns Ok(remaining) if allowed, Err(retry_after_secs) if rate limited
    pub async fn check(&self, ip: IpAddr) -> Result<usize, u64> {
        let now = Instant::now();
        let mut requests = self.requests.write().await;

        let timestamps = requests.entry(ip).or_insert_with(Vec::new);

        // Remove expired timestamps
        timestamps.retain(|&t| now.duration_since(t) < self.window);

        if timestamps.len() >= self.max_requests {
            // Calculate retry after
            if let Some(&oldest) = timestamps.first() {
                let retry_after = self.window.as_secs()
                    - now.duration_since(oldest).as_secs();
                return Err(retry_after.max(1));
            }
            return Err(self.window.as_secs());
        }

        timestamps.push(now);
        Ok(self.max_requests - timestamps.len())
    }

    /// Cleanup old entries periodically (call from background task)
    pub async fn cleanup(&self) {
        let now = Instant::now();
        let mut requests = self.requests.write().await;

        requests.retain(|_, timestamps| {
            timestamps.retain(|&t| now.duration_since(t) < self.window);
            !timestamps.is_empty()
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    #[tokio::test]
    async fn test_rate_limiter() {
        let limiter = RateLimiter::new(3, 60);
        let ip = IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1));

        // First 3 requests should pass
        assert!(limiter.check(ip).await.is_ok());
        assert!(limiter.check(ip).await.is_ok());
        assert!(limiter.check(ip).await.is_ok());

        // 4th request should be rate limited
        assert!(limiter.check(ip).await.is_err());
    }
}
