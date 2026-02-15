use axum::{
    body::Body,
    extract::{Json, Query},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::sync::Arc;
use tokio::sync::Mutex;
use tower_http::cors::{Any, CorsLayer};
use tracing::{error, info};
use uuid::Uuid;

// ============= Request/Response Models =============

#[derive(Deserialize)]
struct DownloadRequest {
    url: String,
}

#[derive(Deserialize)]
struct StreamRequest {
    id: String,
    format: Option<String>,  // Format ID to download (e.g., "http-2176", "best")
}

#[derive(Serialize, Clone)]
struct VideoFormat {
    quality: String,
    resolution: String,
    url: String,  // This will now be the stream URL
    size_bytes: Option<i64>,
    format_id: String,
}

#[derive(Serialize, Clone)]
struct MediaEntry {
    entry_id: String,
    title: Option<String>,
    thumbnail: Option<String>,
    width: Option<i64>,
    height: Option<i64>,
    duration_seconds: Option<f64>,
    duration_formatted: Option<String>,
    media_type: String,
    formats: Vec<VideoFormat>,
    best_url: Option<String>,
}

#[derive(Serialize, Clone)]
struct VideoData {
    platform: String,
    content_type: String,
    video_id: String,
    title: Option<String>,
    description: Option<String>,
    author_name: Option<String>,
    author_username: Option<String>,
    author_avatar: Option<String>,
    thumbnail: Option<String>,
    duration_seconds: Option<f64>,
    duration_formatted: Option<String>,
    stats: serde_json::Value,
    created_at: Option<String>,
    original_url: String,
    is_playlist: bool,
    playlist_count: Option<usize>,
    entries: Vec<MediaEntry>,
}

#[derive(Serialize)]
struct DownloadResponse {
    success: bool,
    message: String,
    session_id: Option<String>,
    expires_in: Option<u64>,
    data: Option<VideoData>,
    video_formats: Vec<VideoFormat>,
    audio_formats: Vec<VideoFormat>,
    image_formats: Vec<VideoFormat>,
    best_video_url: Option<String>,
    best_audio_url: Option<String>,
    best_image_url: Option<String>,
    extracted_at: String,
}

#[derive(Serialize)]
struct ErrorResponse {
    success: bool,
    message: String,
    error_code: Option<String>,
}

#[derive(Serialize)]
struct HealthResponse {
    status: String,
    timestamp: String,
    version: String,
    redis_connected: bool,
}

// ============= Helper Functions =============

fn format_duration(seconds: Option<f64>) -> Option<String> {
    let secs = seconds?;
    if secs <= 0.0 {
        return None;
    }
    let total = secs as u64;
    let h = total / 3600;
    let m = (total % 3600) / 60;
    let s = total % 60;
    if h > 0 {
        Some(format!("{h}:{m:02}:{s:02}"))
    } else {
        Some(format!("{m}:{s:02}"))
    }
}

fn detect_platform(url: &str, extractor: &str) -> String {
    let url_lower = url.to_lowercase();
    let ext_lower = extractor.to_lowercase();
    if url_lower.contains("tiktok.com") || url_lower.contains("douyin.com") {
        "tiktok".into()
    } else if url_lower.contains("twitter.com")
        || url_lower.contains("x.com")
        || ext_lower.contains("twitter")
    {
        "x".into()
    } else {
        "unknown".into()
    }
}

fn now_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

// ============= PyO3 yt-dlp Integration =============

fn extract_with_ytdlp(url: &str) -> Result<String, String> {
    Python::with_gil(|py| {
        let yt_dlp = py.import("yt_dlp").map_err(|e| format!("Failed to import yt_dlp: {e}"))?;

        let opts = PyDict::new(py);
        opts.set_item("quiet", true).unwrap();
        opts.set_item("no_warnings", true).unwrap();
        opts.set_item("extract_flat", false).unwrap();
        opts.set_item("socket_timeout", 30).unwrap();

        let ydl_class = yt_dlp
            .getattr("YoutubeDL")
            .map_err(|e| format!("Failed to get YoutubeDL: {e}"))?;
        let ydl = ydl_class
            .call1((opts,))
            .map_err(|e| format!("Failed to create YoutubeDL: {e}"))?;

        let kwargs = PyDict::new(py);
        kwargs.set_item("download", false).unwrap();
        let info = ydl
            .call_method("extract_info", (url,), Some(&kwargs))
            .map_err(|e| {
                let err_str = e.to_string();
                if err_str.to_lowercase().contains("not found")
                    || err_str.to_lowercase().contains("unable to download")
                {
                    format!("NOT_FOUND:{err_str}")
                } else if err_str.contains("403") || err_str.to_lowercase().contains("forbidden") {
                    format!("FORBIDDEN:{err_str}")
                } else if err_str.to_lowercase().contains("login")
                    || err_str.to_lowercase().contains("authentication")
                {
                    format!("AUTH_REQUIRED:{err_str}")
                } else if err_str.to_lowercase().contains("unsupported url") {
                    format!("UNSUPPORTED:{err_str}")
                } else {
                    format!("EXTRACTION_FAILED:{err_str}")
                }
            })?;

        let json_mod = py
            .import("json")
            .map_err(|e| format!("Failed to import json: {e}"))?;
        let json_str = json_mod
            .call_method1("dumps", (info,))
            .map_err(|e| format!("Failed to serialize: {e}"))?
            .extract::<String>()
            .map_err(|e| format!("Failed to extract string: {e}"))?;

        Ok(json_str)
    })
}

// ============= Format Parsing =============

fn parse_formats(
    formats: &[serde_json::Value],
) -> (Vec<VideoFormat>, Vec<VideoFormat>, Vec<VideoFormat>) {
    let mut video_formats = Vec::new();
    let mut audio_formats = Vec::new();
    let mut image_formats = Vec::new();
    let mut progressive_formats = Vec::new();

    let mut seen_video: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut seen_audio: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut seen_progressive: std::collections::HashSet<i64> = std::collections::HashSet::new();
    let mut seen_image: std::collections::HashSet<String> = std::collections::HashSet::new();

    let audio_re = regex_lite::Regex::new(r"audio-(\d+)").unwrap();

    for fmt in formats {
        let format_id = fmt["format_id"].as_str().unwrap_or("");
        let vcodec = fmt["vcodec"].as_str().unwrap_or("none").to_lowercase();
        let acodec = fmt["acodec"].as_str().unwrap_or("none").to_lowercase();
        let height = fmt["height"].as_i64().unwrap_or(0);
        let width = fmt["width"].as_i64().unwrap_or(0);
        let url = fmt["url"].as_str().unwrap_or("");
        let resolution = fmt["resolution"].as_str().unwrap_or("");
        let video_ext = fmt["video_ext"].as_str().unwrap_or("").to_lowercase();
        let protocol = fmt["protocol"].as_str().unwrap_or("");

        if url.is_empty() {
            continue;
        }

        let is_http = protocol == "https" || (url.starts_with("http") && !url.contains(".m3u8"));
        let is_hls = url.to_lowercase().contains(".m3u8")
            || protocol == "m3u8"
            || protocol == "m3u8_native";

        let is_image = matches!(video_ext.as_str(), "jpg" | "jpeg" | "png" | "webp" | "gif")
            && is_http;
        let is_audio =
            vcodec == "none" && (format_id.to_lowercase().contains("audio") || resolution == "audio only");
        let is_combined = is_http && height > 0 && !is_image;
        let is_video_only = is_hls && vcodec != "none" && height > 0;

        let size_bytes = fmt["filesize"]
            .as_i64()
            .or_else(|| fmt["filesize_approx"].as_i64());

        if is_image {
            let res_str = if width > 0 && height > 0 {
                format!("{width}x{height}")
            } else {
                resolution.to_string()
            };
            let key = format!("{width}x{height}_{format_id}");
            if seen_image.contains(&key) {
                continue;
            }
            seen_image.insert(key);
            let quality = if format_id.is_empty() {
                "IMAGE".into()
            } else {
                format_id.to_uppercase()
            };
            image_formats.push(VideoFormat {
                quality,
                resolution: res_str,
                url: url.to_string(),
                size_bytes,
                format_id: format_id.to_string(),
            });
        } else if is_audio {
            let mut abr = fmt["abr"].as_f64().or_else(|| fmt["tbr"].as_f64()).unwrap_or(0.0);
            if abr == 0.0 {
                if let Some(caps) = audio_re.captures(&format_id.to_lowercase()) {
                    if let Ok(v) = caps[1].parse::<f64>() {
                        abr = v / 1000.0;
                    }
                }
            }
            let quality = if abr > 0.0 {
                format!("{}kbps", abr as i64)
            } else {
                "audio".into()
            };
            if seen_audio.contains(&quality) {
                continue;
            }
            seen_audio.insert(quality.clone());
            audio_formats.push(VideoFormat {
                quality,
                resolution: "audio only".into(),
                url: url.to_string(),
                size_bytes,
                format_id: format_id.to_string(),
            });
        } else if is_combined {
            if seen_progressive.contains(&height) {
                continue;
            }
            seen_progressive.insert(height);
            let res_str = if width > 0 && height > 0 {
                format!("{width}x{height}")
            } else {
                resolution.to_string()
            };
            progressive_formats.push(VideoFormat {
                quality: format!("{height}p (progressive)"),
                resolution: res_str,
                url: url.to_string(),
                size_bytes,
                format_id: format_id.to_string(),
            });
        } else if is_video_only {
            let key = format!("{height}_hls");
            if seen_video.contains(&key) {
                continue;
            }
            seen_video.insert(key);
            let res_str = if width > 0 && height > 0 {
                format!("{width}x{height}")
            } else {
                resolution.to_string()
            };
            video_formats.push(VideoFormat {
                quality: format!("{height}p (hls)"),
                resolution: res_str,
                url: url.to_string(),
                size_bytes,
                format_id: format_id.to_string(),
            });
        }
    }

    let get_height = |f: &VideoFormat| -> i64 {
        f.quality
            .split('p')
            .next()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0)
    };
    progressive_formats.sort_by(|a, b| get_height(b).cmp(&get_height(a)));
    video_formats.sort_by(|a, b| get_height(b).cmp(&get_height(a)));

    let mut all_videos = progressive_formats;
    all_videos.extend(video_formats);

    audio_formats.sort_by(|a, b| {
        let ba = a.quality.replace("kbps", "").parse::<i64>().unwrap_or(0);
        let bb = b.quality.replace("kbps", "").parse::<i64>().unwrap_or(0);
        bb.cmp(&ba)
    });

    let priority = |q: &str| -> i32 {
        match q.to_lowercase().as_str() {
            "orig" => 0,
            "large" => 1,
            "medium" => 2,
            "small" => 3,
            "thumb" => 4,
            _ => 5,
        }
    };
    image_formats.sort_by(|a, b| priority(&a.quality).cmp(&priority(&b.quality)));

    (all_videos, audio_formats, image_formats)
}

// ============= Response Builder =============

#[derive(Serialize, Deserialize, Clone)]
struct FormatInfo {
    url: String,
    http_headers: HashMap<String, String>,
    quality: String,
    resolution: String,
    content_type: String,
}

#[derive(Serialize, Deserialize, Clone)]
struct SessionData {
    video_id: String,
    cookies: Option<String>,
    formats: HashMap<String, FormatInfo>,  // format_id -> FormatInfo
}

async fn store_session_in_redis(
    redis: &mut redis::aio::MultiplexedConnection,
    session_id: &str,
    data: &SessionData,
) -> Result<(), redis::RedisError> {
    let json_data = serde_json::to_string(data).unwrap();
    redis.set_ex(format!("download:{session_id}"), json_data, 300).await?;
    Ok(())
}

async fn get_session_from_redis(
    redis: &mut redis::aio::MultiplexedConnection,
    session_id: &str,
) -> Result<Option<SessionData>, redis::RedisError> {
    let key = format!("download:{session_id}");
    let data: Option<String> = redis.get(&key).await?;
    
    if let Some(json_str) = data {
        // Session will auto-expire after 5 minutes (300s), don't delete immediately
        match serde_json::from_str(&json_str) {
            Ok(session_data) => Ok(Some(session_data)),
            Err(e) => {
                error!("Failed to parse session data: {}", e);
                Ok(None)
            }
        }
    } else {
        Ok(None)
    }
}

fn build_response_with_session(
    info: &serde_json::Value,
    original_url: &str,
    video_fmts: &[VideoFormat],
    audio_fmts: &[VideoFormat],
    image_fmts: &[VideoFormat],
    session_id: &str,
    base_url: &str,
) -> DownloadResponse {
    let platform = detect_platform(
        original_url,
        info["extractor"].as_str().unwrap_or(""),
    );

    let is_playlist = info["_type"].as_str() == Some("playlist");
    let entries = info["entries"].as_array();

    if is_playlist {
        if let Some(entries_arr) = entries {
            if !entries_arr.is_empty() {
                return build_playlist_response(info, entries_arr, &platform, original_url, video_fmts, image_fmts, session_id, base_url);
            }
        }
    }

    let (content_type, message) = if !image_fmts.is_empty() && video_fmts.is_empty() {
        ("photo", "Photo extracted successfully")
    } else if !video_fmts.is_empty() {
        ("video", "Video info extracted successfully")
    } else if !audio_fmts.is_empty() {
        ("audio", "Audio extracted successfully")
    } else {
        ("unknown", "Media extracted successfully")
    };

    // Generate masked URLs with format parameter
    let video_fmts_masked: Vec<VideoFormat> = video_fmts.iter().map(|f| {
        let mut fmt = f.clone();
        fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
        fmt
    }).collect();

    let audio_fmts_masked: Vec<VideoFormat> = audio_fmts.iter().map(|f| {
        let mut fmt = f.clone();
        fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
        fmt
    }).collect();

    let image_fmts_masked: Vec<VideoFormat> = image_fmts.iter().map(|f| {
        let mut fmt = f.clone();
        fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
        fmt
    }).collect();

    let best_video = video_fmts.first().map(|f| format!("{}/stream?id={}&format=best", base_url, session_id));
    let best_audio = audio_fmts.first().map(|f| format!("{}/stream?id={}&format=best_audio", base_url, session_id));
    let best_image = image_fmts.first().map(|f| format!("{}/stream?id={}&format=best_image", base_url, session_id));

    let thumbnail = get_best_thumbnail(info);
    let duration = info["duration"].as_f64();
    let upload_date = info["upload_date"].as_str().unwrap_or("");
    let created_at = parse_upload_date(upload_date);

    let stats = build_stats(info);

    let data = VideoData {
        platform,
        content_type: content_type.into(),
        video_id: info["id"].as_str().unwrap_or("").into(),
        title: str_opt(info, "title").or_else(|| str_opt(info, "fulltitle")),
        description: str_opt(info, "description"),
        author_name: str_opt(info, "uploader"),
        author_username: str_opt(info, "uploader_id"),
        author_avatar: Some(String::new()),
        thumbnail: Some(thumbnail),
        duration_seconds: duration,
        duration_formatted: format_duration(duration),
        stats,
        created_at,
        original_url: original_url.into(),
        is_playlist: false,
        playlist_count: None,
        entries: vec![],
    };

    DownloadResponse {
        success: true,
        message: message.into(),
        session_id: Some(session_id.to_string()),
        expires_in: Some(300),
        data: Some(data),
        video_formats: video_fmts_masked,
        audio_formats: audio_fmts_masked,
        image_formats: image_fmts_masked,
        best_video_url: best_video,
        best_audio_url: best_audio,
        best_image_url: best_image,
        extracted_at: now_utc(),
    }
}

fn build_playlist_response(
    info: &serde_json::Value,
    entries_arr: &[serde_json::Value],
    platform: &str,
    original_url: &str,
    video_fmts: &[VideoFormat],
    image_fmts: &[VideoFormat],
    session_id: &str,
    base_url: &str,
) -> DownloadResponse {
    let mut parsed_entries = Vec::new();

    for (idx, entry) in entries_arr.iter().enumerate() {
        let fmts = entry["formats"].as_array().map(|v| v.as_slice()).unwrap_or(&[]);
        let (vf, _af, imf) = parse_formats(fmts);

        let (media_type, best_url, formats) = if !imf.is_empty() && vf.is_empty() {
            ("photo", imf.first().map(|f| format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id)), 
             imf.iter().map(|f| {
                 let mut fmt = f.clone();
                 fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
                 fmt
             }).collect())
        } else if !vf.is_empty() {
            ("video", vf.first().map(|f| format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id)), 
             vf.iter().map(|f| {
                 let mut fmt = f.clone();
                 fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
                 fmt
             }).collect())
        } else {
            ("unknown", None, vec![])
        };

        let duration = entry["duration"].as_f64();
        let thumb = entry["thumbnail"]
            .as_str()
            .unwrap_or("")
            .to_string();

        parsed_entries.push(MediaEntry {
            entry_id: entry["id"]
                .as_str()
                .map(|s| s.to_string())
                .unwrap_or_else(|| format!("entry_{idx}")),
            title: str_opt(entry, "title").or_else(|| str_opt(entry, "fulltitle")),
            thumbnail: Some(thumb),
            width: entry["width"].as_i64(),
            height: entry["height"].as_i64(),
            duration_seconds: duration,
            duration_formatted: format_duration(duration),
            media_type: media_type.into(),
            formats,
            best_url,
        });
    }

    let content_types: std::collections::HashSet<&str> =
        parsed_entries.iter().map(|e| e.media_type.as_str()).collect();
    let (content_type, message) = if content_types.len() == 1 && content_types.contains("photo") {
        (
            "photo",
            format!(
                "Photo gallery extracted successfully ({} images)",
                parsed_entries.len()
            ),
        )
    } else if content_types.contains("photo") && content_types.contains("video") {
        (
            "mixed",
            format!(
                "Mixed media extracted successfully ({} items)",
                parsed_entries.len()
            ),
        )
    } else {
        (
            "playlist",
            format!(
                "Playlist extracted successfully ({} items)",
                parsed_entries.len()
            ),
        )
    };

    let first = parsed_entries.first();
    
    // Use the passed format lists
    let video_fmts_masked: Vec<VideoFormat> = video_fmts.iter().map(|f| {
        let mut fmt = f.clone();
        fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
        fmt
    }).collect();

    let image_fmts_masked: Vec<VideoFormat> = image_fmts.iter().map(|f| {
        let mut fmt = f.clone();
        fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
        fmt
    }).collect();

    let best_video = video_fmts_masked.first().map(|f| format!("{}/stream?id={}&format=best", base_url, session_id));
    let best_image = image_fmts_masked
        .first()
        .map(|f| format!("{}/stream?id={}&format=best_image", base_url, session_id));

    let created_at = parse_upload_date(info["upload_date"].as_str().unwrap_or(""));
    let stats = build_stats(info);

    let data = VideoData {
        platform: platform.into(),
        content_type: content_type.into(),
        video_id: info["id"].as_str().unwrap_or("").into(),
        title: str_opt(info, "title").or_else(|| str_opt(info, "fulltitle")),
        description: str_opt(info, "description"),
        author_name: str_opt(info, "uploader"),
        author_username: str_opt(info, "uploader_id"),
        author_avatar: Some(String::new()),
        thumbnail: first.and_then(|f| f.thumbnail.clone()),
        duration_seconds: None,
        duration_formatted: None,
        stats,
        created_at,
        original_url: original_url.into(),
        is_playlist: true,
        playlist_count: Some(parsed_entries.len()),
        entries: parsed_entries,
    };

    DownloadResponse {
        success: true,
        message,
        session_id: Some(session_id.to_string()),
        expires_in: Some(300),
        data: Some(data),
        video_formats: video_fmts_masked,
        audio_formats: vec![],
        image_formats: image_fmts_masked,
        best_video_url: best_video,
        best_audio_url: None,
        best_image_url: best_image,
        extracted_at: now_utc(),
    }
}

fn str_opt(v: &serde_json::Value, key: &str) -> Option<String> {
    v[key].as_str().map(|s| s.to_string())
}

fn get_best_thumbnail(info: &serde_json::Value) -> String {
    if let Some(thumbs) = info["thumbnails"].as_array() {
        if let Some(best) = thumbs.iter().max_by_key(|t| {
            let w = t["width"].as_i64().unwrap_or(0);
            let h = t["height"].as_i64().unwrap_or(0);
            w * h
        }) {
            if let Some(url) = best["url"].as_str() {
                return url.to_string();
            }
        }
    }
    info["thumbnail"].as_str().unwrap_or("").to_string()
}

fn parse_upload_date(date: &str) -> Option<String> {
    if date.len() == 8 {
        Some(format!("{}-{}-{}", &date[..4], &date[4..6], &date[6..]))
    } else {
        None
    }
}

fn build_stats(info: &serde_json::Value) -> serde_json::Value {
    let mut map = serde_json::Map::new();
    for (key, field) in [
        ("views", "view_count"),
        ("likes", "like_count"),
        ("comments", "comment_count"),
        ("shares", "repost_count"),
    ] {
        if let Some(v) = info[field].as_i64() {
            map.insert(key.into(), serde_json::Value::Number(v.into()));
        }
    }
    serde_json::Value::Object(map)
}

// ============= API Handlers =============

async fn root() -> impl IntoResponse {
    Json(serde_json::json!({
        "name": "TikTok/X Video Downloader API (Rust)",
        "version": "2.1.0",
        "endpoints": {
            "POST /download": "Extract video/photo info - body: {\"url\": \"media_url\"}",
            "GET /stream?id=xxx": "Stream video using session_id from /download",
            "GET /health": "Health check"
        },
        "supported_platforms": ["TikTok", "X (Twitter)"],
        "runtime": "Rust + Tokio + PyO3 (yt-dlp) + Redis"
    }))
}

async fn health(redis: Arc<Mutex<redis::aio::MultiplexedConnection>>) -> impl IntoResponse {
    let mut redis_guard = redis.lock().await;
    let redis_connected = redis::cmd("PING")
        .query_async::<_, String>(&mut *redis_guard)
        .await
        .is_ok();

    Json(HealthResponse {
        status: "healthy".into(),
        timestamp: now_utc(),
        version: "2.1.0".into(),
        redis_connected,
    })
}

// Helper function to extract headers from format/info
fn extract_headers(format_data: &serde_json::Value, info: &serde_json::Value) -> HashMap<String, String> {
    let mut headers = HashMap::new();
    
    // Get format-level headers
    if let Some(http_headers) = format_data["http_headers"].as_object() {
        for (k, v) in http_headers {
            if let Some(val) = v.as_str() {
                headers.insert(k.clone(), val.to_string());
            }
        }
    }
    
    // Get info-level headers if format headers are empty
    if headers.is_empty() {
        if let Some(http_headers) = info["http_headers"].as_object() {
            for (k, v) in http_headers {
                if let Some(val) = v.as_str() {
                    headers.insert(k.clone(), val.to_string());
                }
            }
        }
    }
    
    headers
}

fn determine_content_type(resolution: &str, format_id: &str, quality: &str) -> String {
    if resolution == "audio only" {
        "audio/mp4".to_string()
    } else if quality.contains("IMAGE") || format_id.to_lowercase().contains("thumb") {
        "image/jpeg".to_string()
    } else {
        "video/mp4".to_string()
    }
}

async fn store_formats_in_session(
    redis: &mut redis::aio::MultiplexedConnection,
    video_fmts: &[VideoFormat],
    audio_fmts: &[VideoFormat],
    image_fmts: &[VideoFormat],
    info: &serde_json::Value,
) -> Result<String, redis::RedisError> {
    let session_id = Uuid::new_v4().to_string();
    let cookies = info["cookies"].as_str().map(|s| s.to_string());
    let video_id = info["id"].as_str().unwrap_or("unknown").to_string();

    let mut formats_map: HashMap<String, FormatInfo> = HashMap::new();

    // Helper closure to process format and add to map
    let mut process_format = |fmt: &VideoFormat, format_data: &serde_json::Value, source_info: &serde_json::Value| {
        let headers = extract_headers(format_data, source_info);
        let content_type = determine_content_type(&fmt.resolution, &fmt.format_id, &fmt.quality);

        let format_info = FormatInfo {
            url: fmt.url.clone(),
            http_headers: headers,
            quality: fmt.quality.clone(),
            resolution: fmt.resolution.clone(),
            content_type,
        };

        formats_map.insert(fmt.format_id.clone(), format_info);
    };

    // Process top-level formats
    for fmt in video_fmts.iter().chain(audio_fmts.iter()).chain(image_fmts.iter()) {
        let format_data = info["formats"]
            .as_array()
            .and_then(|arr| arr.iter().find(|f| f["format_id"].as_str() == Some(&fmt.format_id)))
            .unwrap_or(&serde_json::Value::Null);

        process_format(fmt, format_data, info);
    }

    // Process formats from entries (for playlists/galleries like Twitter/X images)
    if let Some(entries) = info["entries"].as_array() {
        for entry in entries {
            if let Some(entry_formats) = entry["formats"].as_array() {
                for fmt_data in entry_formats {
                    let format_id = fmt_data["format_id"].as_str().unwrap_or("");
                    if format_id.is_empty() {
                        continue;
                    }

                    // Check if this is an image format
                    let video_ext = fmt_data["video_ext"].as_str().unwrap_or("").to_lowercase();
                    let protocol = fmt_data["protocol"].as_str().unwrap_or("");
                    let url = fmt_data["url"].as_str().unwrap_or("");
                    let is_http = protocol == "https" || (url.starts_with("http") && !url.contains(".m3u8"));
                    let is_image = matches!(video_ext.as_str(), "jpg" | "jpeg" | "png" | "webp" | "gif") && is_http;

                    if is_image {
                        let width = fmt_data["width"].as_i64().unwrap_or(0);
                        let height = fmt_data["height"].as_i64().unwrap_or(0);
                        let resolution = if width > 0 && height > 0 {
                            format!("{}x{}", width, height)
                        } else {
                            fmt_data["resolution"].as_str().unwrap_or("").to_string()
                        };

                        let quality = if format_id.is_empty() {
                            "IMAGE".to_string()
                        } else {
                            format_id.to_uppercase()
                        };

                        let size_bytes = fmt_data["filesize"]
                            .as_i64()
                            .or_else(|| fmt_data["filesize_approx"].as_i64());

                        let fmt = VideoFormat {
                            quality: quality.clone(),
                            resolution: resolution.clone(),
                            url: url.to_string(),
                            size_bytes,
                            format_id: format_id.to_string(),
                        };

                        process_format(&fmt, fmt_data, entry);
                    }
                }
            }
        }
    }

    let session_data = SessionData {
        video_id,
        cookies,
        formats: formats_map,
    };

    store_session_in_redis(redis, &session_id, &session_data).await?;
    Ok(session_id)
}

async fn download(
    Json(req): Json<DownloadRequest>,
    redis: Arc<Mutex<redis::aio::MultiplexedConnection>>,
) -> impl IntoResponse {
    let url = req.url.trim().to_string();

    if url.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::to_value(ErrorResponse {
                success: false,
                message: "URL is required".into(),
                error_code: Some("HTTP_400".into()),
            })
            .unwrap()),
        );
    }

    let url_lower = url.to_lowercase();
    let supported = ["tiktok.com", "douyin.com", "twitter.com", "x.com"];
    if !supported.iter().any(|d| url_lower.contains(d)) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::to_value(ErrorResponse {
                success: false,
                message: "Unsupported URL. Only TikTok and X (Twitter) URLs are supported.".into(),
                error_code: Some("HTTP_400".into()),
            })
            .unwrap()),
        );
    }

    let url_clone = url.clone();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(45),
        tokio::task::spawn_blocking(move || extract_with_ytdlp(&url_clone)),
    )
    .await;

    match result {
        Ok(Ok(Ok(json_str))) => {
            match serde_json::from_str::<serde_json::Value>(&json_str) {
                Ok(info) => {
                    let base_url = env::var("BASE_URL").unwrap_or_else(|_| "http://localhost:8025".to_string());
                    let formats_arr = info["formats"].as_array().map(|v| v.as_slice()).unwrap_or(&[]);
                    let (video_fmts, audio_fmts, image_fmts) = parse_formats(formats_arr);
                    
                    // Store all formats in single Redis session
                    let mut redis_guard = redis.lock().await;
                    let session_id = match store_formats_in_session(&mut *redis_guard, &video_fmts, &audio_fmts, &image_fmts, &info).await {
                        Ok(id) => id,
                        Err(e) => {
                            error!("Failed to store session in Redis: {}", e);
                            return (
                                StatusCode::INTERNAL_SERVER_ERROR,
                                Json(serde_json::to_value(ErrorResponse {
                                    success: false,
                                    message: "Failed to create download session".into(),
                                    error_code: Some("REDIS_ERROR".into()),
                                }).unwrap()),
                            );
                        }
                    };
                    drop(redis_guard);
                    
                    let response = build_response_with_session(
                        &info, 
                        &url, 
                        &video_fmts,
                        &audio_fmts,
                        &image_fmts,
                        &session_id,
                        &base_url
                    );
                    
                    (
                        StatusCode::OK,
                        Json(serde_json::to_value(response).unwrap()),
                    )
                }
                Err(e) => {
                    error!("JSON parse error: {e}");
                    (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        Json(serde_json::to_value(ErrorResponse {
                            success: false,
                            message: "Failed to parse extraction result".into(),
                            error_code: Some("INTERNAL_ERROR".into()),
                        })
                        .unwrap()),
                    )
                }
            }
        }
        Ok(Ok(Err(e))) => {
            let (status, msg) = if e.starts_with("NOT_FOUND:") {
                (StatusCode::NOT_FOUND, "Video not found or may be private/deleted")
            } else if e.starts_with("FORBIDDEN:") {
                (StatusCode::FORBIDDEN, "Access forbidden - video may be private or region-restricted")
            } else if e.starts_with("AUTH_REQUIRED:") {
                (StatusCode::UNAUTHORIZED, "This content requires login/authentication")
            } else if e.starts_with("UNSUPPORTED:") {
                (StatusCode::BAD_REQUEST, "Unsupported or invalid URL")
            } else {
                error!("yt-dlp error: {e}");
                (StatusCode::INTERNAL_SERVER_ERROR, "Extraction failed")
            };
            (
                status,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: msg.into(),
                    error_code: Some(format!("HTTP_{}", status.as_u16())),
                })
                .unwrap()),
            )
        }
        Ok(Err(e)) => {
            error!("Task join error: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: "Internal server error".into(),
                    error_code: Some("INTERNAL_ERROR".into()),
                })
                .unwrap()),
            )
        }
        Err(_) => {
            (
                StatusCode::GATEWAY_TIMEOUT,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: "Request timeout - video extraction took too long".into(),
                    error_code: Some("HTTP_504".into()),
                })
                .unwrap()),
            )
        }
    }
}

async fn stream(
    Query(params): Query<StreamRequest>,
    redis: Arc<Mutex<redis::aio::MultiplexedConnection>>,
) -> impl IntoResponse {
    let session_id = params.id;
    let format_id = params.format.unwrap_or_else(|| "best".to_string());
    
    // Get session data from Redis
    let session_data = {
        let mut redis_guard = redis.lock().await;
        match get_session_from_redis(&mut *redis_guard, &session_id).await {
            Ok(data) => data,
            Err(e) => {
                error!("Redis error: {}", e);
                None
            }
        }
    };
    
    let session_data = match session_data {
        Some(data) => data,
        None => {
            return (
                StatusCode::GONE,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: "Session expired or not found. Please extract again.".into(),
                    error_code: Some("SESSION_EXPIRED".into()),
                })
                .unwrap()),
            )
                .into_response();
        }
    };
    
    // Select format based on format_id
    let format_info = match format_id.as_str() {
        "best" => {
            // Find first video format
            session_data.formats.values()
                .find(|f| !f.resolution.is_empty() && f.resolution != "audio only")
                .cloned()
        }
        "best_audio" => {
            // Find first audio format
            session_data.formats.values()
                .find(|f| f.resolution == "audio only")
                .cloned()
        }
        "best_image" => {
            // Find first image format
            session_data.formats.values()
                .find(|f| f.content_type.starts_with("image/"))
                .cloned()
        }
        specific_id => {
            // Look for specific format ID
            session_data.formats.get(specific_id).cloned()
        }
    };
    
    let format_info = match format_info {
        Some(f) => f,
        None => {
            return (
                StatusCode::BAD_REQUEST,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: format!("Format '{}' not found in session", format_id),
                    error_code: Some("FORMAT_NOT_FOUND".into()),
                })
                .unwrap()),
            )
                .into_response();
        }
    };
    
    // Download using reqwest with yt-dlp headers
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(300))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            error!("Failed to build reqwest client: {}", e);
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: "Failed to initialize download client".into(),
                    error_code: Some("CLIENT_ERROR".into()),
                })
                .unwrap()),
            )
                .into_response();
        }
    };
    
    let mut request = client.get(&format_info.url);
    
    // Add headers from yt-dlp
    for (key, value) in &format_info.http_headers {
        if key.to_lowercase() != "cookie" {
            request = request.header(key, value);
        }
    }
    
    // Add Accept-Encoding: identity
    request = request.header("Accept-Encoding", "identity");
    
    // Add cookies if present
    if let Some(cookies) = &session_data.cookies {
        request = request.header("Cookie", cookies);
    }
    
    // Send request
    let response = match request.send().await {
        Ok(resp) => resp,
        Err(e) => {
            error!("Failed to download from URL: {}", e);
            return (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: "Failed to download media from source".into(),
                    error_code: Some("DOWNLOAD_ERROR".into()),
                })
                .unwrap()),
            )
                .into_response();
        }
    };
    
    // Get content type from source or use default
    let content_type = response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or(&format_info.content_type)
        .to_string();
    
    // Generate filename
    let ext = if content_type.starts_with("audio/") {
        "m4a"
    } else if content_type.starts_with("image/") {
        "jpg"
    } else {
        "mp4"
    };
    let filename = format!("{}_{}_{}.{}", 
        session_data.video_id, 
        format_id,
        format_info.quality.replace(|c: char| !c.is_alphanumeric(), "_"),
        ext
    );
    
    // Stream response
    let stream = response.bytes_stream();
    let body = Body::from_stream(stream);
    
    Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", content_type)
        .header(
            "Content-Disposition",
            format!("attachment; filename=\"{}\"", filename),
        )
        .body(body)
        .unwrap()
}

// ============= Main =============

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8025);
    
    let redis_url = env::var("REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".to_string());
    
    // Connect to Redis
    let redis_client = match redis::Client::open(redis_url.clone()) {
        Ok(client) => client,
        Err(e) => {
            error!("Failed to create Redis client: {}", e);
            std::process::exit(1);
        }
    };
    
    let redis_conn = match redis_client.get_multiplexed_async_connection().await {
        Ok(conn) => Arc::new(Mutex::new(conn)),
        Err(e) => {
            error!("Failed to connect to Redis: {}", e);
            std::process::exit(1);
        }
    };

    info!("✅ Connected to Redis at {}", redis_url);

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([axum::http::Method::GET, axum::http::Method::POST])
        .allow_headers(Any);

    let app = Router::new()
        .route("/", get(root))
        .route("/health", get({
            let redis = redis_conn.clone();
            move || health(redis.clone())
        }))
        .route("/download", post({
            let redis = redis_conn.clone();
            move |body| download(body, redis.clone())
        }))
        .route("/stream", get({
            let redis = redis_conn.clone();
            move |query| stream(query, redis.clone())
        }))
        .layer(cors);

    let addr = format!("0.0.0.0:{port}");
    info!("🚀 serverx-rs listening on {addr}");
    info!("   Runtime: Tokio + PyO3 (yt-dlp) + Redis");
    info!("   Endpoints: /download, /stream, /health");

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}