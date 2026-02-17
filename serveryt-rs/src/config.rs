use std::env;
use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct Settings {
    pub port: u16,
    pub base_url: String,
    pub cookies_path: Option<PathBuf>,
    pub ytdlp_timeout: u64,
    pub download_timeout: u64,
    // Signature settings
    pub signature_secret: String,
    pub signature_ttl: u64,
    // Rate limiting
    pub rate_limit_requests: usize,
    pub rate_limit_window: u64,
}

impl Settings {
    pub fn from_env() -> Self {
        Self {
            port: env_parse("PORT", 8026),
            base_url: env_str("BASE_URL", "http://localhost:8026"),
            cookies_path: env::var("COOKIES_PATH").ok().map(PathBuf::from),
            ytdlp_timeout: env_parse("YTDLP_TIMEOUT", 45),
            download_timeout: env_parse("DOWNLOAD_TIMEOUT", 300),
            // Signature secret - MUST be set in production
            signature_secret: env_str("SIGNATURE_SECRET", "change_me_in_production_32chars!"),
            signature_ttl: env_parse("SIGNATURE_TTL", 120), // 2 minutes default
            // Rate limiting: 10 requests per minute per IP
            rate_limit_requests: env_parse("RATE_LIMIT_REQUESTS", 10),
            rate_limit_window: env_parse("RATE_LIMIT_WINDOW", 60),
        }
    }
}

fn env_str(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

fn env_parse<T: std::str::FromStr>(key: &str, default: T) -> T {
    env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
