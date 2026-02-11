use md5::{Digest, Md5};
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use tracing::{debug, info, warn};

#[derive(Clone)]
pub struct RedisCache {
    conn: ConnectionManager,
}

impl RedisCache {
    pub async fn connect(host: &str, port: u16) -> Option<Self> {
        let url = format!("redis://{host}:{port}");
        match redis::Client::open(url.as_str()) {
            Ok(client) => match ConnectionManager::new(client).await {
                Ok(conn) => {
                    info!("✅ Redis connected at {host}:{port}");
                    Some(Self { conn })
                }
                Err(e) => {
                    warn!("⚠️ Redis connection failed: {e}. Caching disabled.");
                    None
                }
            },
            Err(e) => {
                warn!("⚠️ Redis client creation failed: {e}. Caching disabled.");
                None
            }
        }
    }

    pub async fn get_metadata(&self, url: &str) -> Option<String> {
        let cache_key = format!("tiktok:metadata:{}", url_hash(url));
        let mut conn = self.conn.clone();
        match conn.get::<_, Option<String>>(&cache_key).await {
            Ok(Some(cached)) => {
                info!("✅ Cache HIT for {}...", &url[..url.len().min(50)]);
                Some(cached)
            }
            Ok(None) => {
                debug!("Cache MISS for {}...", &url[..url.len().min(50)]);
                None
            }
            Err(e) => {
                warn!("Redis get error: {e}");
                None
            }
        }
    }

    pub async fn set_metadata(&self, url: &str, data: &str, ttl_secs: u64) {
        let cache_key = format!("tiktok:metadata:{}", url_hash(url));
        let mut conn = self.conn.clone();
        if let Err(e) = conn
            .set_ex::<_, _, ()>(&cache_key, data, ttl_secs)
            .await
        {
            warn!("Redis set error: {e}");
        } else {
            debug!(
                "Cached metadata for {}... (TTL: {ttl_secs}s)",
                &url[..url.len().min(50)]
            );
        }
    }

    pub async fn invalidate(&self, url: &str) {
        let cache_key = format!("tiktok:metadata:{}", url_hash(url));
        let mut conn = self.conn.clone();
        if let Err(e) = conn.del::<_, ()>(&cache_key).await {
            warn!("Redis delete error: {e}");
        } else {
            debug!("Invalidated cache for {}...", &url[..url.len().min(50)]);
        }
    }

    pub async fn ping(&self) -> bool {
        let mut conn = self.conn.clone();
        redis::cmd("PING")
            .query_async::<String>(&mut conn)
            .await
            .is_ok()
    }
}

fn url_hash(url: &str) -> String {
    let mut hasher = Md5::new();
    hasher.update(url.as_bytes());
    format!("{:x}", hasher.finalize())
}
