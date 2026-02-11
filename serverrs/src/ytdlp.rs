use pyo3::prelude::*;
use pyo3::types::PyDict;
use tracing::error;

/// Call yt_dlp.YoutubeDL.extract_info() via PyO3 and return raw JSON string.
/// Also extracts per-format cookies from ydl.cookiejar before closing.
/// Runs inside spawn_blocking — Tokio auto-manages the thread pool.
pub fn extract_with_ytdlp(url: &str, cookies_path: Option<&str>) -> Result<String, String> {
    Python::with_gil(|py| {
        let yt_dlp = py
            .import("yt_dlp")
            .map_err(|e| format!("Failed to import yt_dlp: {e}"))?;

        // Build options dict
        let opts = PyDict::new(py);
        opts.set_item("quiet", true).unwrap();
        opts.set_item("no_warnings", true).unwrap();
        opts.set_item("extract_flat", false).unwrap();
        opts.set_item("socket_timeout", 30).unwrap();

        // Add cookies if path exists
        if let Some(cp) = cookies_path {
            if std::path::Path::new(cp).exists() {
                opts.set_item("cookiefile", cp).unwrap();
            }
        }

        // ydl = yt_dlp.YoutubeDL(opts)
        let ydl_class = yt_dlp
            .getattr("YoutubeDL")
            .map_err(|e| format!("Failed to get YoutubeDL: {e}"))?;
        let ydl = ydl_class
            .call1((opts,))
            .map_err(|e| format!("Failed to create YoutubeDL: {e}"))?;

        // info = ydl.extract_info(url, download=False)
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

        // Extract per-format cookies from cookiejar before closing ydl.
        // After extract_info, each format has 'http_headers' but Cookie is stripped.
        // We extract it separately and inject as '_cookies' field.
        let _inject_result: Result<(), String> = (|| {
            let formats = info.get_item("formats").ok().flatten();
            let formats = match formats {
                Some(f) => f,
                None => return Ok(()),
            };
            let cookiejar = match ydl.getattr("cookiejar").ok() {
                Some(cj) => cj,
                None => return Ok(()),
            };

            if let Ok(iter) = formats.iter() {
                for fmt in iter {
                    let fmt = match fmt {
                        Ok(f) => f,
                        Err(_) => continue,
                    };
                    let fmt_url = match fmt.get_item("url").ok().flatten() {
                        Some(u) => u,
                        None => continue,
                    };
                    let cookie_header = match cookiejar
                        .call_method1("get_cookie_header", (fmt_url,))
                        .ok()
                    {
                        Some(ch) => ch,
                        None => continue,
                    };
                    if let Ok(cookie_str) = cookie_header.extract::<String>() {
                        if !cookie_str.is_empty() {
                            let _ = fmt.set_item("_cookies", cookie_str);
                        }
                    }
                }
            }
            Ok(())
        })();

        // Close ydl to release file descriptors
        let _ = ydl.call_method0("close");

        // Convert Python dict to JSON string via json.dumps()
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
