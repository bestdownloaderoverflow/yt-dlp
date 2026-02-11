use std::env;
use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct Settings {
    pub port: u16,
    pub base_url: String,
    pub encryption_key: String,
    pub temp_dir: PathBuf,
    pub cookies_path: PathBuf,
    pub max_workers: usize,
    pub ytdlp_timeout: u64,
    pub download_timeout: u64,
    pub redis_host: String,
    pub redis_port: u16,
    pub instance_id: String,
    pub instance_region: String,
    pub gluetun_control_port: u16,
    pub gluetun_username: String,
    pub gluetun_password: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Self {
            port: env_parse("PORT", 3021),
            base_url: env_str("BASE_URL", "http://localhost:3021"),
            encryption_key: env_str("ENCRYPTION_KEY", "overflow"),
            temp_dir: PathBuf::from(env_str("TEMP_DIR", "./temp")),
            cookies_path: PathBuf::from(env_str(
                "COOKIES_PATH",
                "./cookies/www.tiktok.com_cookies.txt",
            )),
            max_workers: env_parse("MAX_WORKERS", 20),
            ytdlp_timeout: env_parse("YTDLP_TIMEOUT", 30),
            download_timeout: env_parse("DOWNLOAD_TIMEOUT", 120),
            redis_host: env_str("REDIS_HOST", "redis"),
            redis_port: env_parse("REDIS_PORT", 6379),
            instance_id: env_str("INSTANCE_ID", "unknown"),
            instance_region: env_str("INSTANCE_REGION", "unknown"),
            gluetun_control_port: env_parse("GLUETUN_CONTROL_PORT", 8000),
            gluetun_username: env_str("GLUETUN_USERNAME", "admin"),
            gluetun_password: env_str("GLUETUN_PASSWORD", "secretpassword"),
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
