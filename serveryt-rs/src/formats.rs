use serde::Serialize;

#[derive(Serialize, Clone, Debug)]
pub struct VideoFormat {
    pub quality: String,
    pub resolution: String,
    pub url: String,
    pub size_bytes: Option<i64>,
    pub format_id: String,
}

/// Parse yt-dlp formats array into (video, audio, image) format lists.
/// Video formats include both progressive (HTTP with height) and HLS-only.
/// Sorted by quality descending.
pub fn parse_formats(
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
        let _acodec = fmt["acodec"].as_str().unwrap_or("none").to_lowercase();
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
