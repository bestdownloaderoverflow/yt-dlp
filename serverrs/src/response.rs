use serde::Serialize;
use serde_json::Value;

use crate::config::Settings;
use crate::encryption::encrypt;

#[derive(Serialize)]
pub struct AuthorInfo {
    pub nickname: String,
    #[serde(rename = "uniqueId")]
    pub unique_id: String,
    pub signature: String,
    pub avatar: String,
    #[serde(rename = "avatarThumb")]
    pub avatar_thumb: String,
    #[serde(rename = "avatarMedium")]
    pub avatar_medium: String,
    #[serde(rename = "avatarLarger")]
    pub avatar_larger: String,
}

#[derive(Serialize)]
pub struct Statistics {
    pub play_count: i64,
    pub digg_count: i64,
    pub comment_count: i64,
    pub share_count: i64,
}

/// Generate JSON response matching serverpy format.
/// Returns a serde_json::Value with status "picker" (images) or "tunnel" (video).
pub fn generate_json_response(data: &Value, url: &str, settings: &Settings) -> Value {
    let formats = data["formats"].as_array();

    let is_image = formats
        .map(|fmts| {
            fmts.iter().any(|f| {
                f["format_id"]
                    .as_str()
                    .unwrap_or("")
                    .starts_with("image-")
            })
        })
        .unwrap_or(false);

    // Extract author info
    let avatar_url = data["thumbnails"]
        .as_array()
        .and_then(|t| t.first())
        .and_then(|t| t["url"].as_str())
        .unwrap_or("")
        .to_string();

    let nickname = str_or(data, "uploader", str_or(data, "channel", "unknown".into()));
    let unique_id = str_or(data, "uploader_id", nickname.clone());

    let author = AuthorInfo {
        nickname: nickname.clone(),
        unique_id,
        signature: str_or(data, "description", String::new()),
        avatar: avatar_url.clone(),
        avatar_thumb: avatar_url.clone(),
        avatar_medium: avatar_url.clone(),
        avatar_larger: avatar_url,
    };

    let statistics = Statistics {
        play_count: data["view_count"].as_i64().unwrap_or(0),
        digg_count: data["like_count"].as_i64().unwrap_or(0),
        comment_count: data["comment_count"].as_i64().unwrap_or(0),
        share_count: data["repost_count"].as_i64().unwrap_or(0),
    };

    let duration_ms = data["duration"].as_f64().map(|d| (d * 1000.0) as i64).unwrap_or(0);
    let title = str_or(data, "title", str_or(data, "fulltitle", String::new()));
    let description = str_or(data, "description", title.clone());
    let artist = str_or(data, "artist", nickname.clone());
    let cover = str_or(data, "thumbnail", String::new());

    let mut base = serde_json::json!({
        "title": title,
        "description": description,
        "statistics": serde_json::to_value(&statistics).unwrap(),
        "artist": artist,
        "cover": cover,
        "duration": duration_ms,
        "audio": "",
        "download_link": {},
        "music_duration": duration_ms,
        "author": serde_json::to_value(&author).unwrap(),
    });

    if is_image {
        build_image_response(&mut base, data, url, &author.nickname, settings)
    } else {
        build_video_response(&mut base, data, &author.nickname, settings)
    }
}

fn build_image_response(
    base: &mut Value,
    data: &Value,
    url: &str,
    author_nickname: &str,
    settings: &Settings,
) -> Value {
    let formats = data["formats"].as_array().unwrap();
    let image_formats: Vec<&Value> = formats
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

    // Build picker array
    let picker: Vec<Value> = image_formats
        .iter()
        .map(|img| {
            serde_json::json!({
                "type": "photo",
                "url": img["url"].as_str().unwrap_or("")
            })
        })
        .collect();

    if let Some(af) = audio_format {
        base["audio"] = Value::String(af["url"].as_str().unwrap_or("").to_string());
    }

    // Create encrypted download links for images
    let encrypted_image_urls: Vec<Value> = image_formats
        .iter()
        .map(|img| {
            let payload = serde_json::json!({
                "url": img["url"].as_str().unwrap_or(""),
                "author": author_nickname,
                "type": "image"
            });
            let encrypted = encrypt(
                &payload.to_string(),
                &settings.encryption_key,
                Some(360),
            );
            Value::String(format!("{}/download?data={encrypted}", settings.base_url))
        })
        .collect();

    let mut download_link = serde_json::json!({
        "no_watermark": encrypted_image_urls
    });

    // Audio download link
    if let Some(af) = audio_format {
        let mut audio_stream_headers = serde_json::Map::new();
        if let Some(headers) = af["http_headers"].as_object() {
            for (k, v) in headers {
                audio_stream_headers.insert(k.clone(), v.clone());
            }
        }
        if let Some(cookies) = af["_cookies"].as_str() {
            audio_stream_headers
                .insert("Cookie".to_string(), Value::String(cookies.to_string()));
        }

        let payload = serde_json::json!({
            "url": af["url"].as_str().unwrap_or(""),
            "author": author_nickname,
            "filesize": af["filesize"].as_i64().unwrap_or(0),
            "http_headers": Value::Object(audio_stream_headers),
            "type": "mp3"
        });
        let encrypted = encrypt(
            &payload.to_string(),
            &settings.encryption_key,
            Some(360),
        );
        download_link["mp3"] = Value::String(format!("{}/stream?data={encrypted}", settings.base_url));
    }

    base["download_link"] = download_link;

    // Slideshow download link
    let encrypted_url = encrypt(url, &settings.encryption_key, Some(360));
    base["download_slideshow_link"] =
        Value::String(format!("{}/download-slideshow?url={encrypted_url}", settings.base_url));

    let mut result = serde_json::json!({ "status": "picker", "photos": picker });
    // Merge base into result
    if let (Some(result_obj), Some(base_obj)) = (result.as_object_mut(), base.as_object()) {
        for (k, v) in base_obj {
            result_obj.insert(k.clone(), v.clone());
        }
    }
    result
}

fn build_video_response(
    base: &mut Value,
    data: &Value,
    author_nickname: &str,
    settings: &Settings,
) -> Value {
    let empty_vec = Vec::new();
    let formats = data["formats"].as_array().unwrap_or(&empty_vec).clone();

    // Video formats: has both vcodec and acodec
    let mut video_formats: Vec<&Value> = formats
        .iter()
        .filter(|f| {
            let vcodec = f["vcodec"].as_str().unwrap_or("none");
            let acodec = f["acodec"].as_str().unwrap_or("none");
            vcodec != "none" && acodec != "none"
        })
        .collect();

    // Audio-only format
    let audio_format = formats
        .iter()
        .find(|f| {
            let acodec = f["acodec"].as_str().unwrap_or("none");
            let vcodec = f["vcodec"].as_str().unwrap_or("none");
            acodec != "none" && (vcodec == "none" || vcodec.is_empty())
        })
        .or_else(|| video_formats.first().copied());

    if let Some(af) = audio_format {
        base["audio"] = Value::String(af["url"].as_str().unwrap_or("").to_string());
    }

    // Sort by quality (height * width) descending
    video_formats.sort_by(|a, b| {
        let qa = a["height"].as_i64().unwrap_or(0) * a["width"].as_i64().unwrap_or(0);
        let qb = b["height"].as_i64().unwrap_or(0) * b["width"].as_i64().unwrap_or(0);
        qb.cmp(&qa)
    });

    let download_format = formats
        .iter()
        .find(|f| f["format_id"].as_str() == Some("download"));
    let hd_formats: Vec<&&Value> = video_formats
        .iter()
        .filter(|f| f["height"].as_i64().unwrap_or(0) >= 720)
        .collect();
    let sd_formats: Vec<&&Value> = video_formats
        .iter()
        .filter(|f| f["height"].as_i64().unwrap_or(0) < 720)
        .collect();

    let mut download_link = serde_json::Map::new();

    if let Some(df) = download_format {
        if let Some(link) = gen_stream_link(df, author_nickname, "video", settings) {
            download_link.insert("watermark".to_string(), Value::String(link));
        }
    }

    if let Some(sd) = sd_formats.first() {
        if let Some(link) = gen_stream_link(sd, author_nickname, "video", settings) {
            download_link.insert("no_watermark".to_string(), Value::String(link));
        }
    }

    if let Some(hd) = hd_formats.first() {
        if let Some(link) = gen_stream_link(hd, author_nickname, "video", settings) {
            download_link.insert("no_watermark_hd".to_string(), Value::String(link));
        }
        if hd_formats.len() > 1 {
            if let Some(link) = gen_stream_link(hd_formats[1], author_nickname, "video", settings) {
                download_link.insert("watermark_hd".to_string(), Value::String(link));
            }
        }
    }

    if let Some(af) = audio_format {
        if let Some(link) = gen_stream_link(af, author_nickname, "mp3", settings) {
            download_link.insert("mp3".to_string(), Value::String(link));
        }
    }

    base["download_link"] = Value::Object(download_link);

    let mut result = serde_json::json!({ "status": "tunnel" });
    if let (Some(result_obj), Some(base_obj)) = (result.as_object_mut(), base.as_object()) {
        for (k, v) in base_obj {
            result_obj.insert(k.clone(), v.clone());
        }
    }
    result
}

/// Generate an encrypted stream link for a format.
fn gen_stream_link(
    format_obj: &Value,
    author_nickname: &str,
    file_type: &str,
    settings: &Settings,
) -> Option<String> {
    let url = format_obj["url"].as_str()?;

    let filesize = format_obj["filesize"]
        .as_i64()
        .or_else(|| format_obj["filesize_approx"].as_i64())
        .unwrap_or(0);

    let mut stream_headers = serde_json::Map::new();
    if let Some(headers) = format_obj["http_headers"].as_object() {
        for (k, v) in headers {
            stream_headers.insert(k.clone(), v.clone());
        }
    }
    if let Some(cookies) = format_obj["_cookies"].as_str() {
        stream_headers.insert("Cookie".to_string(), Value::String(cookies.to_string()));
    }

    let payload = serde_json::json!({
        "url": url,
        "author": author_nickname,
        "filesize": filesize,
        "http_headers": Value::Object(stream_headers),
        "type": file_type
    });

    let encrypted = encrypt(
        &payload.to_string(),
        &settings.encryption_key,
        Some(360),
    );
    Some(format!("{}/stream?data={encrypted}", settings.base_url))
}

fn str_or(v: &Value, key: &str, default: String) -> String {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or(default)
}
