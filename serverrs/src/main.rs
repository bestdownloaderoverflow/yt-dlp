mod cache;
mod cleanup;
mod config;
mod encryption;
mod response;
mod slideshow;
mod stream;
mod vpn;
mod ytdlp;

use axum::body::Body;
use axum::extract::{Json, Query, State};
use axum::http::{HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use serde::Deserialize;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;
use tower_http::cors::{Any, CorsLayer};
use tracing::{error, info, warn};

use cache::RedisCache;
use config::Settings;
use encryption::decrypt;
use vpn::{VpnManager, VpnReconnectState};

// ============= Application State =============

#[derive(Clone)]
pub struct AppState {
    pub settings: Settings,
    pub http_client: reqwest::Client,
    pub redis: Option<RedisCache>,
    pub vpn_manager: Arc<VpnManager>,
    pub vpn_state: Arc<Mutex<VpnReconnectState>>,
}

// ============= Request/Response Models =============

#[derive(Deserialize)]
struct TikTokRequest {
    url: String,
}

#[derive(Deserialize)]
struct SlideshowQuery {
    url: String,
}

// ============= Handlers =============

/// POST /tiktok — Process TikTok URL and return metadata with encrypted download links
async fn tiktok_handler(
    State(state): State<AppState>,
    Json(req): Json<TikTokRequest>,
) -> impl IntoResponse {
    let url = req.url.trim().to_string();

    if url.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "URL parameter is required"})),
        )
            .into_response();
    }

    let url_lower = url.to_lowercase();
    if !url_lower.contains("tiktok.com") && !url_lower.contains("douyin.com") {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "Only TikTok and Douyin URLs are supported"})),
        )
            .into_response();
    }

    // Fetch data (with cache)
    let data = match fetch_tiktok_data(&url, &state).await {
        Ok(d) => d,
        Err(resp) => return resp,
    };

    // Generate response
    let response = response::generate_json_response(&data, &url, &state.settings);
    (StatusCode::OK, Json(response)).into_response()
}

/// GET /download — Download file using encrypted data
async fn download_handler(
    State(state): State<AppState>,
    Query(query): Query<stream::DownloadQuery>,
) -> impl IntoResponse {
    stream::download_handler(Query(query), state.settings, state.http_client).await
}

/// GET /stream — Stream video/audio directly
async fn stream_handler(
    State(state): State<AppState>,
    Query(query): Query<stream::DownloadQuery>,
) -> impl IntoResponse {
    stream::stream_handler(Query(query), state.settings, state.http_client).await
}

/// GET /download-slideshow — Generate and download slideshow video from image post
async fn slideshow_handler(
    State(state): State<AppState>,
    Query(query): Query<SlideshowQuery>,
) -> impl IntoResponse {
    if query.url.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "URL parameter is required"})),
        )
            .into_response();
    }

    // Decrypt URL
    let decrypted_url = match decrypt(&query.url, &state.settings.encryption_key) {
        Ok(u) => u,
        Err(e) => {
            error!("Decryption failed: {e}");
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": format!("Decryption failed: {e}")})),
            )
                .into_response();
        }
    };

    // Fetch TikTok data
    let data = match fetch_tiktok_data(&decrypted_url, &state).await {
        Ok(d) => d,
        Err(resp) => return resp,
    };

    // Check if it's an image post
    let is_image = data["formats"]
        .as_array()
        .map(|fmts| {
            fmts.iter().any(|f| {
                f["format_id"]
                    .as_str()
                    .unwrap_or("")
                    .starts_with("image-")
            })
        })
        .unwrap_or(false);

    if !is_image {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "Only image posts are supported"})),
        )
            .into_response();
    }

    let formats = match data["formats"].as_array() {
        Some(f) => f.clone(),
        None => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": "Invalid response from yt-dlp"})),
            )
                .into_response()
        }
    };

    let image_formats: Vec<&serde_json::Value> = formats
        .iter()
        .filter(|f| {
            f["format_id"]
                .as_str()
                .unwrap_or("")
                .starts_with("image-")
        })
        .collect();
    let audio_format = formats
        .iter()
        .find(|f| f["format_id"].as_str() == Some("audio"));

    if image_formats.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": "No images found"})),
        )
            .into_response();
    }

    let audio_url = match audio_format.and_then(|af| af["url"].as_str()) {
        Some(u) if !u.is_empty() => u.to_string(),
        _ => {
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({"error": "Could not find audio URL"})),
            )
                .into_response()
        }
    };

    let image_urls: Vec<String> = image_formats
        .iter()
        .filter_map(|f| f["url"].as_str().map(|s| s.to_string()))
        .collect();

    // Create work directory
    let video_id = data["id"].as_str().unwrap_or("unknown");
    let author_id = data["uploader_id"].as_str().unwrap_or("unknown");
    let now_ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis();
    let folder_name = format!("{video_id}_{author_id}_{now_ts}");
    let work_dir = state.settings.temp_dir.join(&folder_name);

    if let Err(e) = std::fs::create_dir_all(&work_dir) {
        error!("Failed to create work dir: {e}");
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to create work dir: {e}")})),
        )
            .into_response();
    }

    let work_dir_str = work_dir.to_string_lossy().to_string();
    let audio_path = work_dir.join("audio.mp3").to_string_lossy().to_string();
    let output_path = work_dir.join("slideshow.mp4").to_string_lossy().to_string();

    // Download audio and images in spawn_blocking
    let audio_url_clone = audio_url.clone();
    let audio_path_clone = audio_path.clone();
    let dl_result = tokio::task::spawn_blocking(move || {
        slideshow::download_file(&audio_url_clone, &audio_path_clone, 120)
    })
    .await;

    if let Err(e) = dl_result.unwrap_or(Err("Task join error".into())) {
        error!("Failed to download audio: {e}");
        let wd = work_dir_str.clone();
        tokio::task::spawn_blocking(move || cleanup::cleanup_folder(&wd));
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Failed to download audio: {e}")})),
        )
            .into_response();
    }

    let mut image_paths = Vec::new();
    for (i, img_url) in image_urls.iter().enumerate() {
        let img_path = work_dir
            .join(format!("image_{i}.jpg"))
            .to_string_lossy()
            .to_string();
        let url_clone = img_url.clone();
        let path_clone = img_path.clone();
        let dl_result = tokio::task::spawn_blocking(move || {
            slideshow::download_file(&url_clone, &path_clone, 120)
        })
        .await;

        if let Err(e) = dl_result.unwrap_or(Err("Task join error".into())) {
            error!("Failed to download image {i}: {e}");
            let wd = work_dir_str.clone();
            tokio::task::spawn_blocking(move || cleanup::cleanup_folder(&wd));
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": format!("Failed to download image: {e}")})),
            )
                .into_response();
        }
        image_paths.push(img_path);
    }

    // Create slideshow
    let imgs = image_paths.clone();
    let ap = audio_path.clone();
    let op = output_path.clone();
    let ss_result =
        tokio::task::spawn_blocking(move || slideshow::create_slideshow(&imgs, &ap, &op, 4)).await;

    if let Err(e) = ss_result.unwrap_or(Err("Task join error".into())) {
        error!("Slideshow creation failed: {e}");
        let wd = work_dir_str.clone();
        tokio::task::spawn_blocking(move || cleanup::cleanup_folder(&wd));
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({"error": format!("Slideshow creation failed: {e}")})),
        )
            .into_response();
    }

    // Read output file and stream it
    let author_nickname = data["uploader"]
        .as_str()
        .or_else(|| data["channel"].as_str())
        .unwrap_or("unknown");
    let sanitized: String = author_nickname
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    let filename = format!("{sanitized}_{now_ts}.mp4");

    let file_bytes = match tokio::fs::read(&output_path).await {
        Ok(b) => b,
        Err(e) => {
            error!("Failed to read output file: {e}");
            let wd = work_dir_str.clone();
            tokio::task::spawn_blocking(move || cleanup::cleanup_folder(&wd));
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": "Failed to read slideshow output"})),
            )
                .into_response();
        }
    };

    // Schedule cleanup
    let wd = work_dir_str.clone();
    tokio::task::spawn(async move {
        tokio::task::spawn_blocking(move || cleanup::cleanup_folder(&wd))
            .await
            .ok();
    });

    let body = Body::from(file_bytes);
    let mut resp = Response::new(body);
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut().insert(
        "Content-Type",
        HeaderValue::from_static("video/mp4"),
    );
    resp.headers_mut().insert(
        "Content-Disposition",
        HeaderValue::from_str(&format!("attachment; filename=\"{filename}\"")).unwrap(),
    );
    resp
}

/// GET /health — Health check endpoint
async fn health_handler(State(state): State<AppState>) -> impl IntoResponse {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();

    let redis_status = if let Some(ref redis) = state.redis {
        if redis.ping().await {
            "connected"
        } else {
            "error"
        }
    } else {
        "disconnected"
    };

    // Check VPN connectivity if configured
    let mut health = serde_json::json!({
        "status": "ok",
        "instance_id": state.settings.instance_id,
        "instance_region": state.settings.instance_region,
        "port": state.settings.port,
        "timestamp": now,
        "runtime": "Rust + Tokio + PyO3 (yt-dlp)",
        "redis": {
            "status": redis_status,
            "caching_enabled": state.redis.is_some()
        }
    });

    if state.settings.gluetun_control_port != 8000 {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .ok();

        if let Some(client) = client {
            match client
                .get(format!(
                    "http://localhost:{}/v1/publicip/ip",
                    state.settings.gluetun_control_port
                ))
                .basic_auth(
                    &state.settings.gluetun_username,
                    Some(&state.settings.gluetun_password),
                )
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    if let Ok(ip_data) = resp.json::<serde_json::Value>().await {
                        health["vpn"] = serde_json::json!({
                            "public_ip": ip_data["public_ip"],
                            "status": "connected"
                        });
                    }
                }
                Ok(resp) => {
                    health["vpn"] = serde_json::json!({
                        "status": "error",
                        "error": format!("HTTP {}", resp.status())
                    });
                }
                Err(e) => {
                    health["vpn"] = serde_json::json!({
                        "status": "error",
                        "error": e.to_string()
                    });
                }
            }
        }
    }

    (StatusCode::OK, Json(health))
}

/// 404 handler
async fn not_found_handler() -> impl IntoResponse {
    (
        StatusCode::NOT_FOUND,
        Json(serde_json::json!({"error": "Route not found"})),
    )
}

// ============= Core Logic =============

/// Fetch TikTok data via yt-dlp with Redis caching
async fn fetch_tiktok_data(
    url: &str,
    state: &AppState,
) -> Result<serde_json::Value, axum::response::Response> {
    // Check cache first
    if let Some(ref redis) = state.redis {
        if let Some(cached) = redis.get_metadata(url).await {
            if let Ok(data) = serde_json::from_str(&cached) {
                return Ok(data);
            }
        }
    }

    // Cache miss — extract via yt-dlp
    let url_clone = url.to_string();
    let cookies_path = state.settings.cookies_path.to_string_lossy().to_string();
    let timeout_secs = state.settings.ytdlp_timeout;

    let result = tokio::time::timeout(
        std::time::Duration::from_secs(timeout_secs),
        tokio::task::spawn_blocking(move || {
            ytdlp::extract_with_ytdlp(&url_clone, Some(&cookies_path))
        }),
    )
    .await;

    match result {
        Ok(Ok(Ok(json_str))) => {
            let data: serde_json::Value = serde_json::from_str(&json_str).map_err(|e| {
                error!("JSON parse error: {e}");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(serde_json::json!({"error": "Failed to parse extraction result"})),
                )
                    .into_response()
            })?;

            // Cache the result
            if let Some(ref redis) = state.redis {
                redis.set_metadata(url, &json_str, 300).await;
            }

            Ok(data)
        }
        Ok(Ok(Err(e))) => {
            // yt-dlp error
            let (status, msg) = if e.starts_with("NOT_FOUND:") {
                (
                    StatusCode::NOT_FOUND,
                    "Video not found. Please check the URL and make sure the video exists.",
                )
            } else if e.starts_with("FORBIDDEN:") {
                // Trigger VPN reconnect
                warn!("403 Forbidden detected on {}, triggering VPN reconnect", state.settings.instance_id);
                let _ = vpn::trigger_local_vpn_reconnect(
                    &state.vpn_state,
                    &state.settings.instance_id,
                    state.settings.gluetun_control_port,
                    &state.settings.gluetun_username,
                    &state.settings.gluetun_password,
                )
                .await;
                (
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Service temporarily unavailable due to IP block, retrying with different endpoint",
                )
            } else if e.starts_with("AUTH_REQUIRED:") {
                (
                    StatusCode::UNAUTHORIZED,
                    "This content requires login/authentication",
                )
            } else if e.starts_with("UNSUPPORTED:") {
                (StatusCode::BAD_REQUEST, "Unsupported or invalid URL")
            } else {
                error!("yt-dlp error: {e}");
                (StatusCode::INTERNAL_SERVER_ERROR, "Extraction failed")
            };
            Err((status, Json(serde_json::json!({"error": msg}))).into_response())
        }
        Ok(Err(e)) => {
            error!("Task join error: {e}");
            Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({"error": "Internal server error"})),
            )
                .into_response())
        }
        Err(_) => {
            // Timeout
            Err((
                StatusCode::REQUEST_TIMEOUT,
                Json(serde_json::json!({"error": "Request timeout after extraction took too long"})),
            )
                .into_response())
        }
    }
}

// ============= Main =============

#[tokio::main]
async fn main() {
    // Setup logging
    tracing_subscriber::fmt::init();

    let settings = Settings::from_env();

    // Ensure temp directory exists
    std::fs::create_dir_all(&settings.temp_dir).ok();

    info!("Starting server on port {}", settings.port);
    info!("Base URL: {}", settings.base_url);
    info!("Temp directory: {:?}", settings.temp_dir);
    info!(
        "Instance: {} ({})",
        settings.instance_id, settings.instance_region
    );

    // Initialize HTTP client with connection pooling
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(settings.download_timeout))
        .connect_timeout(std::time::Duration::from_secs(10))
        .pool_max_idle_per_host(20)
        .redirect(reqwest::redirect::Policy::limited(10))
        .build()
        .expect("Failed to create HTTP client");

    // Initialize Redis
    let redis = RedisCache::connect(&settings.redis_host, settings.redis_port).await;

    // Initialize VPN manager
    let vpn_manager = Arc::new(VpnManager::new(
        settings.gluetun_username.clone(),
        settings.gluetun_password.clone(),
    ));

    // Start cleanup scheduler
    cleanup::spawn_cleanup_task(settings.temp_dir.to_string_lossy().to_string());

    let state = AppState {
        settings: settings.clone(),
        http_client,
        redis,
        vpn_manager,
        vpn_state: Arc::new(Mutex::new(VpnReconnectState::default())),
    };

    // CORS
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([
            axum::http::Method::GET,
            axum::http::Method::POST,
            axum::http::Method::OPTIONS,
        ])
        .allow_headers(Any)
        .expose_headers([
            "Content-Disposition".parse().unwrap(),
            "X-Filename".parse().unwrap(),
            "Content-Length".parse().unwrap(),
        ]);

    // Router
    let app = Router::new()
        .route("/tiktok", post(tiktok_handler))
        .route("/download", get(download_handler))
        .route("/stream", get(stream_handler))
        .route("/download-slideshow", get(slideshow_handler))
        .route("/health", get(health_handler))
        .fallback(not_found_handler)
        .layer(cors)
        .with_state(state);

    let addr = format!("0.0.0.0:{}", settings.port);
    info!("🚀 serverrs listening on {addr}");
    info!("   Runtime: Tokio (auto-managed thread pool)");
    info!("   Extraction: yt-dlp via PyO3");

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
