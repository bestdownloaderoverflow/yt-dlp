use serde::Serialize;

use crate::formats::{parse_formats, VideoFormat};
use crate::signature::generate_stream_token;

// ============= Response Models =============

#[derive(Serialize, Clone)]
pub struct MediaEntry {
    pub entry_id: String,
    pub title: Option<String>,
    pub thumbnail: Option<String>,
    pub width: Option<i64>,
    pub height: Option<i64>,
    pub duration_seconds: Option<f64>,
    pub duration_formatted: Option<String>,
    pub media_type: String,
    pub formats: Vec<VideoFormat>,
    pub best_url: Option<String>,
}

#[derive(Serialize, Clone)]
pub struct VideoData {
    pub platform: String,
    pub content_type: String,
    pub video_id: String,
    pub title: Option<String>,
    pub description: Option<String>,
    pub author_name: Option<String>,
    pub author_username: Option<String>,
    pub author_avatar: Option<String>,
    pub thumbnail: Option<String>,
    pub duration_seconds: Option<f64>,
    pub duration_formatted: Option<String>,
    pub stats: serde_json::Value,
    pub created_at: Option<String>,
    pub original_url: String,
    pub is_playlist: bool,
    pub playlist_count: Option<usize>,
    pub entries: Vec<MediaEntry>,
}

#[derive(Serialize)]
pub struct DownloadResponse {
    pub success: bool,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    pub expires_in: u64,
    pub data: Option<VideoData>,
    pub video_formats: Vec<VideoFormat>,
    pub audio_formats: Vec<VideoFormat>,
    pub image_formats: Vec<VideoFormat>,
    pub best_video_url: Option<String>,
    pub best_audio_url: Option<String>,
    pub best_image_url: Option<String>,
    pub extracted_at: String,
}

#[derive(Serialize)]
pub struct ErrorResponse {
    pub success: bool,
    pub message: String,
    pub error_code: Option<String>,
}

#[derive(Serialize)]
pub struct HealthResponse {
    pub status: String,
    pub timestamp: String,
    pub version: String,
}

// ============= Helper Functions =============

pub fn format_duration(seconds: Option<f64>) -> Option<String> {
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

pub fn detect_platform(url: &str, extractor: &str) -> String {
    let url_lower = url.to_lowercase();
    let ext_lower = extractor.to_lowercase();
    if url_lower.contains("tiktok.com") || url_lower.contains("douyin.com") {
        "tiktok".into()
    } else if url_lower.contains("twitter.com")
        || url_lower.contains("x.com")
        || ext_lower.contains("twitter")
    {
        "x".into()
    } else if url_lower.contains("youtube.com") || url_lower.contains("youtu.be") {
        "youtube".into()
    } else if url_lower.contains("instagram.com") {
        "instagram".into()
    } else if url_lower.contains("facebook.com") || url_lower.contains("fb.watch") {
        "facebook".into()
    } else {
        ext_lower
            .split(':')
            .next()
            .unwrap_or("unknown")
            .to_string()
    }
}

pub fn now_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
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

// ============= Response Builders =============

/// Generate a signed stream URL with expiry
fn make_signed_url(base_url: &str, original_url: &str, format_id: &str, secret: &str) -> String {
    let (sig, expires) = generate_stream_token(original_url, format_id, secret);
    format!(
        "{}/stream?url={}&format={}&expires={}&sig={}",
        base_url,
        urlencoding::encode(original_url),
        urlencoding::encode(format_id),
        expires,
        sig
    )
}

/// Build response with signed URLs (stateless, no Redis)
pub fn build_signed_response(
    info: &serde_json::Value,
    original_url: &str,
    video_fmts: &[VideoFormat],
    audio_fmts: &[VideoFormat],
    image_fmts: &[VideoFormat],
    base_url: &str,
    secret: &str,
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
                return build_signed_playlist_response(
                    info,
                    entries_arr,
                    &platform,
                    original_url,
                    video_fmts,
                    image_fmts,
                    base_url,
                    secret,
                );
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

    // Create signed URLs for each format
    let video_fmts_signed: Vec<VideoFormat> = video_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = make_signed_url(base_url, original_url, &f.format_id, secret);
            fmt
        })
        .collect();

    let audio_fmts_signed: Vec<VideoFormat> = audio_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = make_signed_url(base_url, original_url, &f.format_id, secret);
            fmt
        })
        .collect();

    let image_fmts_signed: Vec<VideoFormat> = image_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = make_signed_url(base_url, original_url, &f.format_id, secret);
            fmt
        })
        .collect();

    let best_video = if !video_fmts.is_empty() {
        Some(make_signed_url(base_url, original_url, "bestvideo+bestaudio/best", secret))
    } else {
        None
    };
    let best_audio = if !audio_fmts.is_empty() {
        Some(make_signed_url(base_url, original_url, "bestaudio/best", secret))
    } else {
        None
    };
    let best_image = if !image_fmts.is_empty() {
        Some(make_signed_url(base_url, original_url, "best", secret))
    } else {
        None
    };

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
        session_id: None,  // Stateless - no session
        expires_in: 120,   // 2 minutes
        data: Some(data),
        video_formats: video_fmts_signed,
        audio_formats: audio_fmts_signed,
        image_formats: image_fmts_signed,
        best_video_url: best_video,
        best_audio_url: best_audio,
        best_image_url: best_image,
        extracted_at: now_utc(),
    }
}

fn build_signed_playlist_response(
    info: &serde_json::Value,
    entries_arr: &[serde_json::Value],
    platform: &str,
    original_url: &str,
    video_fmts: &[VideoFormat],
    image_fmts: &[VideoFormat],
    base_url: &str,
    secret: &str,
) -> DownloadResponse {
    let mut parsed_entries = Vec::new();

    for (idx, entry) in entries_arr.iter().enumerate() {
        let entry_url = entry["webpage_url"]
            .as_str()
            .or_else(|| entry["url"].as_str())
            .unwrap_or(original_url);

        let fmts = entry["formats"]
            .as_array()
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        let (vf, _af, imf) = parse_formats(fmts);

        let (media_type, best_url, formats) = if !imf.is_empty() && vf.is_empty() {
            (
                "photo",
                imf.first().map(|f| make_signed_url(base_url, entry_url, &f.format_id, secret)),
                imf.iter()
                    .map(|f| {
                        let mut fmt = f.clone();
                        fmt.url = make_signed_url(base_url, entry_url, &f.format_id, secret);
                        fmt
                    })
                    .collect(),
            )
        } else if !vf.is_empty() {
            (
                "video",
                Some(make_signed_url(base_url, entry_url, "bestvideo+bestaudio/best", secret)),
                vf.iter()
                    .map(|f| {
                        let mut fmt = f.clone();
                        fmt.url = make_signed_url(base_url, entry_url, &f.format_id, secret);
                        fmt
                    })
                    .collect(),
            )
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

    let video_fmts_signed: Vec<VideoFormat> = video_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = make_signed_url(base_url, original_url, &f.format_id, secret);
            fmt
        })
        .collect();

    let image_fmts_signed: Vec<VideoFormat> = image_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = make_signed_url(base_url, original_url, &f.format_id, secret);
            fmt
        })
        .collect();

    let best_video = if !video_fmts_signed.is_empty() {
        Some(make_signed_url(base_url, original_url, "bestvideo+bestaudio/best", secret))
    } else {
        None
    };
    let best_image = if !image_fmts_signed.is_empty() {
        Some(make_signed_url(base_url, original_url, "best", secret))
    } else {
        None
    };

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
        thumbnail: parsed_entries.first().and_then(|f| f.thumbnail.clone()),
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
        session_id: None,  // Stateless
        expires_in: 120,   // 2 minutes
        data: Some(data),
        video_formats: video_fmts_signed,
        audio_formats: vec![],
        image_formats: image_fmts_signed,
        best_video_url: best_video,
        best_audio_url: None,
        best_image_url: best_image,
        extracted_at: now_utc(),
    }
}

pub fn build_response(
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
                return build_playlist_response(
                    info,
                    entries_arr,
                    &platform,
                    original_url,
                    video_fmts,
                    image_fmts,
                    session_id,
                    base_url,
                );
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

    let video_fmts_masked: Vec<VideoFormat> = video_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
            fmt
        })
        .collect();

    let audio_fmts_masked: Vec<VideoFormat> = audio_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
            fmt
        })
        .collect();

    let image_fmts_masked: Vec<VideoFormat> = image_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
            fmt
        })
        .collect();

    let best_video = video_fmts
        .first()
        .map(|_| format!("{}/stream?id={}&format=best", base_url, session_id));
    let best_audio = audio_fmts
        .first()
        .map(|_| format!("{}/stream?id={}&format=best_audio", base_url, session_id));
    let best_image = image_fmts
        .first()
        .map(|_| format!("{}/stream?id={}&format=best_image", base_url, session_id));

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
        expires_in: 300,
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
        let entry_id = entry["id"].as_str().unwrap_or("");
        let fmts = entry["formats"]
            .as_array()
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        let (vf, _af, imf) = parse_formats(fmts);

        let prefixed_format_id = |format_id: &str| -> String {
            if entry_id.is_empty() {
                format_id.to_string()
            } else {
                format!("{}_{}", entry_id, format_id)
            }
        };

        let (media_type, best_url, formats) = if !imf.is_empty() && vf.is_empty() {
            (
                "photo",
                imf.first().map(|f| {
                    format!(
                        "{}/stream?id={}&format={}",
                        base_url,
                        session_id,
                        prefixed_format_id(&f.format_id)
                    )
                }),
                imf.iter()
                    .map(|f| {
                        let mut fmt = f.clone();
                        fmt.url = format!(
                            "{}/stream?id={}&format={}",
                            base_url,
                            session_id,
                            prefixed_format_id(&f.format_id)
                        );
                        fmt
                    })
                    .collect(),
            )
        } else if !vf.is_empty() {
            (
                "video",
                vf.first().map(|f| {
                    format!(
                        "{}/stream?id={}&format={}",
                        base_url,
                        session_id,
                        prefixed_format_id(&f.format_id)
                    )
                }),
                vf.iter()
                    .map(|f| {
                        let mut fmt = f.clone();
                        fmt.url = format!(
                            "{}/stream?id={}&format={}",
                            base_url,
                            session_id,
                            prefixed_format_id(&f.format_id)
                        );
                        fmt
                    })
                    .collect(),
            )
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

    let video_fmts_masked: Vec<VideoFormat> = video_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
            fmt
        })
        .collect();

    let image_fmts_masked: Vec<VideoFormat> = image_fmts
        .iter()
        .map(|f| {
            let mut fmt = f.clone();
            fmt.url = format!("{}/stream?id={}&format={}", base_url, session_id, f.format_id);
            fmt
        })
        .collect();

    let best_video = video_fmts_masked
        .first()
        .map(|_| format!("{}/stream?id={}&format=best", base_url, session_id));
    let best_image = image_fmts_masked
        .first()
        .map(|_| format!("{}/stream?id={}&format=best_image", base_url, session_id));

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
        thumbnail: parsed_entries.first().and_then(|f| f.thumbnail.clone()),
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
        expires_in: 300,
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
