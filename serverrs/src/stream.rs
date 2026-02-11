use axum::body::Body;
use axum::extract::Query;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use futures_util::StreamExt;
use serde::Deserialize;
use tracing::error;

use crate::config::Settings;
use crate::encryption::decrypt;

#[derive(Deserialize)]
pub struct DownloadQuery {
    pub data: String,
}

/// Content type mapping
fn content_type_info(file_type: &str) -> (&str, &str) {
    match file_type {
        "mp3" => ("audio/mpeg", "mp3"),
        "video" => ("video/mp4", "mp4"),
        "image" => ("image/jpeg", "jpg"),
        _ => ("application/octet-stream", "bin"),
    }
}

/// Sanitize author name for filename (iOS compatible)
fn safe_filename(author: &str, ext: &str) -> String {
    let safe: String = author
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '_' { c } else { '_' })
        .collect();
    format!("{safe}.{ext}")
}

/// GET /download — Download file using encrypted data token
pub async fn download_handler(
    Query(query): Query<DownloadQuery>,
    settings: Settings,
    http_client: reqwest::Client,
) -> impl IntoResponse {
    if query.data.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            "Encrypted data parameter is required",
        )
            .into_response();
    }

    let decrypted = match decrypt(&query.data, &settings.encryption_key) {
        Ok(d) => d,
        Err(e) => {
            error!("Decryption failed: {e}");
            return (StatusCode::BAD_REQUEST, format!("Decryption failed: {e}")).into_response();
        }
    };

    let download_data: serde_json::Value = match serde_json::from_str(&decrypted) {
        Ok(d) => d,
        Err(e) => {
            error!("JSON parse failed: {e}");
            return (StatusCode::BAD_REQUEST, "Invalid decrypted data").into_response();
        }
    };

    let author = match download_data["author"].as_str() {
        Some(a) if !a.is_empty() => a,
        _ => {
            return (
                StatusCode::BAD_REQUEST,
                "Invalid decrypted data: missing author",
            )
                .into_response()
        }
    };
    let file_type = match download_data["type"].as_str() {
        Some(t) if !t.is_empty() => t,
        _ => {
            return (
                StatusCode::BAD_REQUEST,
                "Invalid decrypted data: missing type",
            )
                .into_response()
        }
    };
    let url = match download_data["url"].as_str() {
        Some(u) if !u.is_empty() => u.to_string(),
        _ => return (StatusCode::BAD_REQUEST, "No download URL provided").into_response(),
    };

    let (content_type, ext) = content_type_info(file_type);
    let filename = safe_filename(author, ext);

    stream_from_cdn(http_client, &url, None, content_type, &filename, download_data["filesize"].as_i64()).await
}

/// GET /stream — Stream video/audio directly via pre-extracted CDN URL + auth headers
pub async fn stream_handler(
    Query(query): Query<DownloadQuery>,
    settings: Settings,
    http_client: reqwest::Client,
) -> impl IntoResponse {
    if query.data.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            "Encrypted data parameter is required",
        )
            .into_response();
    }

    let decrypted = match decrypt(&query.data, &settings.encryption_key) {
        Ok(d) => d,
        Err(e) => {
            error!("Decryption failed: {e}");
            return (StatusCode::BAD_REQUEST, format!("Decryption failed: {e}")).into_response();
        }
    };

    let stream_data: serde_json::Value = match serde_json::from_str(&decrypted) {
        Ok(d) => d,
        Err(e) => {
            error!("JSON parse failed: {e}");
            return (StatusCode::BAD_REQUEST, "Invalid decrypted data").into_response();
        }
    };

    let url = match stream_data["url"].as_str() {
        Some(u) if !u.is_empty() => u.to_string(),
        _ => {
            return (
                StatusCode::BAD_REQUEST,
                "Invalid decrypted data: missing url",
            )
                .into_response()
        }
    };
    let author = match stream_data["author"].as_str() {
        Some(a) if !a.is_empty() => a,
        _ => {
            return (
                StatusCode::BAD_REQUEST,
                "Invalid decrypted data: missing author",
            )
                .into_response()
        }
    };

    let file_type = stream_data["type"].as_str().unwrap_or("video");
    let (content_type, ext) = if file_type == "mp3" || file_type == "audio" {
        ("audio/mpeg", "mp3")
    } else {
        ("video/mp4", "mp4")
    };
    let filename = safe_filename(author, ext);

    // Build request headers from pre-extracted auth data
    let req_headers = stream_data["http_headers"].as_object().cloned();

    stream_from_cdn(
        http_client,
        &url,
        req_headers,
        content_type,
        &filename,
        stream_data["filesize"].as_i64(),
    )
    .await
}

/// Stream content from CDN URL, proxying through our server
async fn stream_from_cdn(
    http_client: reqwest::Client,
    url: &str,
    req_headers: Option<serde_json::Map<String, serde_json::Value>>,
    content_type: &str,
    filename: &str,
    filesize: Option<i64>,
) -> Response {
    let mut request = http_client.get(url);

    // Forward pre-extracted headers (Referer, Cookie, etc.)
    if let Some(headers) = req_headers {
        for (k, v) in &headers {
            if let Some(val) = v.as_str() {
                if let (Ok(name), Ok(value)) = (
                    HeaderName::try_from(k.as_str()),
                    HeaderValue::from_str(val),
                ) {
                    request = request.header(name, value);
                }
            }
        }
    }

    let response = match request.send().await {
        Ok(r) => r,
        Err(e) => {
            error!("HTTP error streaming from CDN: {e}");
            return (StatusCode::BAD_GATEWAY, format!("CDN request failed: {e}")).into_response();
        }
    };

    if !response.status().is_success() {
        error!(
            "CDN returned status {} for {}",
            response.status(),
            &url[..url.len().min(80)]
        );
        return (
            StatusCode::BAD_GATEWAY,
            format!("CDN returned status {}", response.status()),
        )
            .into_response();
    }

    // Build response headers
    let mut resp_headers = HeaderMap::new();
    resp_headers.insert(
        "Content-Disposition",
        HeaderValue::from_str(&format!("attachment; filename=\"{filename}\"")).unwrap(),
    );
    resp_headers.insert(
        "X-Filename",
        HeaderValue::from_str(filename).unwrap_or_else(|_| HeaderValue::from_static("download")),
    );
    resp_headers.insert("Cache-Control", HeaderValue::from_static("no-cache"));

    // Content-Length from token or upstream
    if let Some(size) = filesize {
        if size > 0 {
            resp_headers.insert(
                "Content-Length",
                HeaderValue::from_str(&size.to_string()).unwrap(),
            );
        }
    }
    if !resp_headers.contains_key("Content-Length") {
        if let Some(cl) = response.headers().get("content-length") {
            resp_headers.insert("Content-Length", cl.clone());
        }
    }

    // Stream body
    let stream = response.bytes_stream().map(|result| {
        result.map_err(|e| {
            error!("Error streaming chunk: {e}");
            std::io::Error::new(std::io::ErrorKind::Other, e)
        })
    });

    let body = Body::from_stream(stream);

    let mut resp = Response::new(body);
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert("Content-Type", HeaderValue::from_str(content_type).unwrap());
    for (k, v) in resp_headers {
        if let Some(key) = k {
            resp.headers_mut().insert(key, v);
        }
    }
    resp
}
