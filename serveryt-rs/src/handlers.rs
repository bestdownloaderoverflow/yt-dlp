use axum::body::Body;
use axum::extract::{ConnectInfo, Json, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tracing::{error, info, warn};

use crate::formats::parse_formats;
use crate::response::{self, ErrorResponse};
use crate::signature;
use crate::ytdlp;
use crate::AppState;

// ============= Request Models =============

#[derive(Deserialize)]
pub struct DownloadRequest {
    pub url: String,
}

#[derive(Deserialize)]
pub struct StreamQuery {
    pub url: String,
    pub format: String,
    pub expires: u64,
    pub sig: String,
}

#[derive(Serialize)]
pub struct HealthResponse {
    pub status: String,
    pub timestamp: String,
    pub version: String,
}

// ============= Handlers =============

/// GET / — Root info
pub async fn root() -> impl IntoResponse {
    Json(serde_json::json!({
        "name": "yt-dlp Video Downloader API (Rust)",
        "version": "2.0.0",
        "endpoints": {
            "POST /download": "Extract video info and get signed stream URLs - body: {\"url\": \"media_url\"}",
            "GET /stream?url=xxx&format=yyy&expires=ts&sig=xxx": "Stream media with signed URL (2 min expiry)",
            "GET /health": "Health check"
        },
        "supported_platforms": "All yt-dlp supported sites",
        "runtime": "Rust + Tokio + PyO3 (yt-dlp) - Stateless"
    }))
}

/// GET /health — Health check
pub async fn health() -> impl IntoResponse {
    Json(HealthResponse {
        status: "healthy".into(),
        timestamp: response::now_utc(),
        version: "2.0.0".into(),
    })
}

/// POST /download — Extract info via yt-dlp, return signed stream URLs (stateless)
pub async fn download(
    State(state): State<AppState>,
    Json(req): Json<DownloadRequest>,
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
        )
            .into_response();
    }

    // Extract via yt-dlp in spawn_blocking
    let url_clone = url.clone();
    let cookies_path = state
        .settings
        .cookies_path
        .as_ref()
        .map(|p| p.to_string_lossy().to_string());

    let result = tokio::time::timeout(
        std::time::Duration::from_secs(state.settings.ytdlp_timeout),
        tokio::task::spawn_blocking(move || {
            crate::ytdlp::extract_with_ytdlp(&url_clone, cookies_path.as_deref())
        }),
    )
    .await;

    match result {
        Ok(Ok(Ok(json_str))) => {
            match serde_json::from_str::<serde_json::Value>(&json_str) {
                Ok(info) => {
                    let formats_arr = info["formats"]
                        .as_array()
                        .map(|v| v.as_slice())
                        .unwrap_or(&[]);
                    let (video_fmts, audio_fmts, image_fmts) = parse_formats(formats_arr);

                    // Build response with signed URLs (stateless)
                    let resp = response::build_signed_response(
                        &info,
                        &url,
                        &video_fmts,
                        &audio_fmts,
                        &image_fmts,
                        &state.settings.base_url,
                        &state.settings.signature_secret,
                    );

                    let total_formats = video_fmts.len() + audio_fmts.len() + image_fmts.len();
                    info!(
                        "Extracted {} formats for {}",
                        total_formats,
                        &url[..url.len().min(60)]
                    );

                    (
                        StatusCode::OK,
                        Json(serde_json::to_value(resp).unwrap()),
                    )
                        .into_response()
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
                        .into_response()
                }
            }
        }
        Ok(Ok(Err(e))) => {
            let (status, msg) = if e.starts_with("NOT_FOUND:") {
                (
                    StatusCode::NOT_FOUND,
                    "Video not found or may be private/deleted",
                )
            } else if e.starts_with("FORBIDDEN:") {
                (
                    StatusCode::FORBIDDEN,
                    "Access forbidden - video may be private or region-restricted",
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
            (
                status,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: msg.into(),
                    error_code: Some(format!("HTTP_{}", status.as_u16())),
                })
                .unwrap()),
            )
                .into_response()
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
                .into_response()
        }
        Err(_) => (
            StatusCode::GATEWAY_TIMEOUT,
            Json(serde_json::to_value(ErrorResponse {
                success: false,
                message: "Request timeout - video extraction took too long".into(),
                error_code: Some("HTTP_504".into()),
            })
            .unwrap()),
        )
            .into_response(),
    }
}

/// GET /stream — Stream media with signature validation.
/// Uses reqwest for direct HTTP URLs (fast, native Rust, no GIL).
/// Falls back to yt-dlp download for m3u8/HLS formats.
pub async fn stream(
    State(state): State<AppState>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    Query(params): Query<StreamQuery>,
) -> impl IntoResponse {
    let url = params.url;
    let format_id = params.format;
    let expires = params.expires;
    let sig = params.sig;

    // Rate limiting check
    match state.rate_limiter.check(addr.ip()).await {
        Ok(remaining) => {
            info!("Rate limit OK for {}: {} remaining", addr.ip(), remaining);
        }
        Err(retry_after) => {
            warn!("Rate limited: {} (retry after {}s)", addr.ip(), retry_after);
            return Response::builder()
                .status(StatusCode::TOO_MANY_REQUESTS)
                .header("Retry-After", retry_after.to_string())
                .body(Body::from(
                    serde_json::to_string(&ErrorResponse {
                        success: false,
                        message: format!("Rate limited. Retry after {} seconds", retry_after),
                        error_code: Some("RATE_LIMITED".into()),
                    })
                    .unwrap(),
                ))
                .unwrap()
                .into_response();
        }
    }

    // Verify signature and expiry
    if let Err(e) = signature::verify_stream_token(
        &url,
        &format_id,
        expires,
        &sig,
        &state.settings.signature_secret,
    ) {
        let (status, msg) = match e {
            signature::SignatureError::Expired => {
                (StatusCode::GONE, "Link expired. Please request a new download link.")
            }
            signature::SignatureError::Invalid => {
                (StatusCode::FORBIDDEN, "Invalid signature")
            }
        };
        warn!("Signature verification failed for {}: {}", addr.ip(), e);
        return (
            status,
            Json(serde_json::to_value(ErrorResponse {
                success: false,
                message: msg.into(),
                error_code: Some("SIGNATURE_ERROR".into()),
            })
            .unwrap()),
        )
            .into_response();
    }

    // Determine content type based on format
    let content_type = if format_id.contains("audio") || format_id == "bestaudio" {
        "audio/mp4"
    } else {
        "video/mp4"
    };

    let ext = if content_type.starts_with("audio/") {
        "m4a"
    } else {
        "mp4"
    };

    // Generate filename from URL
    let video_id = url
        .split(&['/', '=', '?'][..])
        .filter(|s| s.len() >= 8 && s.len() <= 16)
        .last()
        .unwrap_or("video");
    let filename = format!("{}_{}.{}", video_id, format_id.replace('+', "_"), ext);

    let original_url = url.clone();
    let format_selector = format_id.clone();
    let cookies_path = state
        .settings
        .cookies_path
        .as_ref()
        .map(|p| p.to_string_lossy().to_string());

    // Step 1: Extract stream info (direct URL, headers, protocol) via yt-dlp
    let cookies_for_extract = cookies_path.clone();
    let url_for_extract = original_url.clone();
    let fmt_for_extract = format_selector.clone();

    let stream_info = match tokio::task::spawn_blocking(move || {
        ytdlp::extract_stream_info(
            &url_for_extract,
            Some(&fmt_for_extract),
            cookies_for_extract.as_deref(),
        )
    })
    .await
    {
        Ok(Ok(info)) => info,
        Ok(Err(e)) => {
            error!("Stream info extraction failed: {e}");
            let (status, msg) = if e.starts_with("NOT_FOUND:") {
                (StatusCode::NOT_FOUND, "Video not found")
            } else if e.starts_with("FORBIDDEN:") {
                (StatusCode::FORBIDDEN, "Access forbidden")
            } else {
                (StatusCode::INTERNAL_SERVER_ERROR, "Stream extraction failed")
            };
            return (
                status,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: msg.into(),
                    error_code: Some(format!("HTTP_{}", status.as_u16())),
                })
                .unwrap()),
            )
                .into_response();
        }
        Err(e) => {
            error!("Task join error: {e}");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::to_value(ErrorResponse {
                    success: false,
                    message: "Internal server error".into(),
                    error_code: Some("INTERNAL_ERROR".into()),
                })
                .unwrap()),
            )
                .into_response();
        }
    };

    // Step 2: Choose streaming strategy based on protocol
    if stream_info.is_hls {
        // m3u8/HLS: use FFmpeg for true streaming without temp files
        info!(
            "HLS stream detected, using FFmpeg for {} format {}",
            &original_url[..original_url.len().min(60)],
            &format_selector
        );
        stream_via_ffmpeg(stream_info, content_type, &filename).await
    } else {
        // Direct HTTP: stream via reqwest (fast, no GIL, no FFI)
        info!(
            "Direct HTTP stream via reqwest for {} format {}",
            &original_url[..original_url.len().min(60)],
            &format_selector
        );
        stream_via_reqwest(stream_info, content_type, &filename).await
    }
}

/// Stream directly from CDN using reqwest — no Python/GIL involved.
async fn stream_via_reqwest(
    stream_info: ytdlp::StreamInfo,
    content_type: &str,
    filename: &str,
) -> Response {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .connect_timeout(std::time::Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::limited(10))
        .build()
        .unwrap_or_default();

    let mut req = client.get(&stream_info.direct_url);

    // Forward all headers from yt-dlp (Referer, User-Agent, Cookie, etc.)
    for (key, val) in &stream_info.http_headers {
        req = req.header(key.as_str(), val.as_str());
    }

    let upstream = match req.send().await {
        Ok(resp) => {
            if !resp.status().is_success() {
                error!(
                    "CDN returned HTTP {} for {}",
                    resp.status(),
                    &stream_info.direct_url[..stream_info.direct_url.len().min(80)]
                );
                return Response::builder()
                    .status(StatusCode::BAD_GATEWAY)
                    .body(Body::from(
                        serde_json::to_string(&ErrorResponse {
                            success: false,
                            message: format!("CDN returned HTTP {}", resp.status()),
                            error_code: Some("BAD_GATEWAY".into()),
                        })
                        .unwrap(),
                    ))
                    .unwrap();
            }
            resp
        }
        Err(e) => {
            error!("reqwest error: {e}");
            return Response::builder()
                .status(StatusCode::BAD_GATEWAY)
                .body(Body::from(
                    serde_json::to_string(&ErrorResponse {
                        success: false,
                        message: "Failed to connect to media server".into(),
                        error_code: Some("BAD_GATEWAY".into()),
                    })
                    .unwrap(),
                ))
                .unwrap();
        }
    };

    // Forward Content-Length from upstream if available
    let content_length = upstream
        .content_length()
        .or(stream_info.filesize.map(|s| s as u64));

    // Stream reqwest response body directly to axum response
    let byte_stream = upstream.bytes_stream().map(|result| {
        result.map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))
    });
    let body = Body::from_stream(byte_stream);

    let mut builder = Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", content_type)
        .header(
            "Content-Disposition",
            format!("attachment; filename=\"{}\"", filename),
        )
        .header("Cache-Control", "no-cache, no-store");

    if let Some(len) = content_length {
        builder = builder.header("Content-Length", len.to_string());
    }

    builder.body(body).unwrap()
}

/// Stream HLS/m3u8 via FFmpeg — uses subprocess for true streaming without temp files.
async fn stream_via_ffmpeg(
    stream_info: ytdlp::StreamInfo,
    content_type: &str,
    filename: &str,
) -> Response {
    let (async_tx, async_rx) =
        tokio::sync::mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(256);

    let hls_url = stream_info.direct_url;
    let headers = stream_info.http_headers;

    tokio::task::spawn_blocking(move || {
        let (sync_tx, sync_rx) = mpsc::channel::<Result<Vec<u8>, String>>();

        // Bridge thread: sync_rx -> async_tx
        let async_tx_clone = async_tx.clone();
        let bridge_handle = std::thread::spawn(move || {
            while let Ok(result) = sync_rx.recv() {
                match result {
                    Ok(data) => {
                        if async_tx_clone
                            .blocking_send(Ok(bytes::Bytes::from(data)))
                            .is_err()
                        {
                            break;
                        }
                    }
                    Err(e) => {
                        let _ = async_tx_clone.blocking_send(Err(std::io::Error::new(
                            std::io::ErrorKind::Other,
                            e,
                        )));
                        break;
                    }
                }
            }
        });

        let result = ytdlp::stream_hls_with_ffmpeg(&hls_url, &headers, sync_tx);

        match result {
            Ok(()) => {
                info!("FFmpeg HLS streaming completed successfully");
            }
            Err(e) => {
                error!("FFmpeg HLS streaming error: {e}");
            }
        }

        let _ = bridge_handle.join();
    });

    let stream = ReceiverStream::new(async_rx);
    let body = Body::from_stream(stream);

    Response::builder()
        .status(StatusCode::OK)
        .header("Content-Type", content_type)
        .header(
            "Content-Disposition",
            format!("attachment; filename=\"{}\"", filename),
        )
        .header("Cache-Control", "no-cache, no-store")
        .header("Transfer-Encoding", "chunked")
        .body(body)
        .unwrap()
}
