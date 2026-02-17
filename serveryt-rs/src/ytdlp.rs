use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashMap;
use std::sync::mpsc;

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
        // Enable Deno JS runtime for yt-dlp-ejs (required for full YouTube support)
        let js_runtimes = PyDict::new(py);
        js_runtimes.set_item("deno", PyDict::new(py)).unwrap();
        opts.set_item("js_runtimes", &js_runtimes).unwrap();

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
            let formats = match info.get_item("formats").ok() {
                Some(f) => f,
                None => return Ok(()),
            };
            let cookiejar = match ydl.getattr("cookiejar").ok() {
                Some(cj) => cj,
                None => return Ok(()),
            };

            if let Ok(iter) = formats.try_iter() {
                for fmt in iter {
                    let fmt = match fmt {
                        Ok(f) => f,
                        Err(_) => continue,
                    };
                    let fmt_url = match fmt.get_item("url").ok() {
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

/// Info needed to stream a specific format directly from CDN.
#[derive(Debug, Clone)]
pub struct StreamInfo {
    pub direct_url: String,
    pub http_headers: HashMap<String, String>,
    pub is_hls: bool,
    pub filesize: Option<i64>,
}

/// Extract stream info (direct URL, cookies, headers, protocol) for a given format.
/// Uses yt-dlp extract_info + format selection to resolve the final URL.
/// For direct HTTP URLs, the caller can use reqwest to stream.
/// For m3u8/HLS, the caller should fall back to download_with_ytdlp.
pub fn extract_stream_info(
    url: &str,
    format_selector: Option<&str>,
    cookies_path: Option<&str>,
) -> Result<StreamInfo, String> {
    Python::with_gil(|py| {
        let yt_dlp = py
            .import("yt_dlp")
            .map_err(|e| format!("Failed to import yt_dlp: {e}"))?;

        let opts = PyDict::new(py);
        opts.set_item("quiet", true).unwrap();
        opts.set_item("no_warnings", true).unwrap();
        opts.set_item("extract_flat", false).unwrap();
        opts.set_item("socket_timeout", 30).unwrap();
        // Enable Deno JS runtime for yt-dlp-ejs (required for full YouTube support)
        let js_runtimes = PyDict::new(py);
        js_runtimes.set_item("deno", PyDict::new(py)).unwrap();
        opts.set_item("js_runtimes", &js_runtimes).unwrap();

        if let Some(fmt) = format_selector {
            opts.set_item("format", fmt).unwrap();
        }

        if let Some(cp) = cookies_path {
            if std::path::Path::new(cp).exists() {
                opts.set_item("cookiefile", cp).unwrap();
            }
        }

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
                classify_error(&err_str)
            })?;

        // Helper: extract a string field from a PyAny dict-like object
        fn py_str(obj: &Bound<'_, pyo3::types::PyAny>, key: &str) -> Option<String> {
            obj.get_item(key).ok().and_then(|v| v.extract::<String>().ok())
        }
        fn py_i64(obj: &Bound<'_, pyo3::types::PyAny>, key: &str) -> Option<i64> {
            obj.get_item(key).ok().and_then(|v| v.extract::<i64>().ok())
        }
        fn extract_headers(obj: &Bound<'_, pyo3::types::PyAny>, headers: &mut HashMap<String, String>) {
            if let Ok(hdrs) = obj.get_item("http_headers") {
                if let Ok(dict) = hdrs.downcast::<PyDict>() {
                    for (k, v) in dict.iter() {
                        if let (Ok(key), Ok(val)) = (k.extract::<String>(), v.extract::<String>()) {
                            headers.insert(key, val);
                        }
                    }
                }
            }
        }
        fn extract_cookies(ydl: &Bound<'_, pyo3::types::PyAny>, url: &str, headers: &mut HashMap<String, String>) {
            if let Ok(cj) = ydl.getattr("cookiejar") {
                if let Ok(cookie_str) = cj
                    .call_method1("get_cookie_header", (url,))
                    .and_then(|c| c.extract::<String>())
                {
                    if !cookie_str.is_empty() {
                        headers.insert("Cookie".to_string(), cookie_str);
                    }
                }
            }
        }

        // yt-dlp with format selector populates 'url' at top level for the selected format.
        // Also check 'requested_formats' for merged format selection.
        let mut http_headers = HashMap::new();

        // Try top-level url first (single format selection)
        let top_url = py_str(&info, "url").unwrap_or_default();
        let top_protocol = py_str(&info, "protocol").unwrap_or_default();

        if !top_url.is_empty() {
            let is_hls = top_url.contains(".m3u8")
                || top_protocol == "m3u8"
                || top_protocol == "m3u8_native";
            let filesize = py_i64(&info, "filesize")
                .or_else(|| py_i64(&info, "filesize_approx"));

            extract_headers(&info, &mut http_headers);
            extract_cookies(&ydl, &top_url, &mut http_headers);

            let _ = ydl.call_method0("close");
            return Ok(StreamInfo {
                direct_url: top_url,
                http_headers,
                is_hls,
                filesize,
            });
        }

        // Fallback: check requested_formats (merged format like bestvideo+bestaudio)
        // Pick the first requested_format (usually video) for streaming
        if let Ok(req_fmts) = info.get_item("requested_formats") {
            if let Ok(iter) = req_fmts.try_iter() {
                for fmt_result in iter {
                    let fmt = match fmt_result {
                        Ok(f) => f,
                        Err(_) => continue,
                    };
                    let fmt_url = py_str(&fmt, "url").unwrap_or_default();
                    if fmt_url.is_empty() {
                        continue;
                    }
                    let fmt_protocol = py_str(&fmt, "protocol").unwrap_or_default();

                    let is_hls = fmt_url.contains(".m3u8")
                        || fmt_protocol == "m3u8"
                        || fmt_protocol == "m3u8_native";
                    let filesize = py_i64(&fmt, "filesize")
                        .or_else(|| py_i64(&fmt, "filesize_approx"));

                    extract_headers(&fmt, &mut http_headers);
                    extract_cookies(&ydl, &fmt_url, &mut http_headers);

                    let _ = ydl.call_method0("close");
                    return Ok(StreamInfo {
                        direct_url: fmt_url,
                        http_headers,
                        is_hls,
                        filesize,
                    });
                }
            }
        }

        let _ = ydl.call_method0("close");
        Err("EXTRACTION_FAILED:No stream URL found in extracted info".to_string())
    })
}

/// Download video via yt-dlp using direct streaming through PyO3.
/// Used as fallback for m3u8/HLS formats that cannot be streamed directly.
/// Uses yt-dlp's native `output_stream` parameter that pipes chunks to Rust.
pub fn download_with_ytdlp(
    url: &str,
    format_selector: Option<&str>,
    cookies_path: Option<&str>,
    tx: mpsc::Sender<Result<Vec<u8>, String>>,
) -> Result<(), String> {
    Python::with_gil(|py| {
        let sender_wrapper = RustSenderWrapper { tx: tx.clone() };

        let locals = PyDict::new(py);
        locals
            .set_item(
                "rust_sender",
                sender_wrapper.into_pyobject(py).map_err(|e| format!("PyO3 error: {e}"))?,
            )
            .unwrap();

        // StreamBridge: file-like object that forwards writes to Rust channel.
        // Sends each write immediately (no batching) to avoid stalls.
        py.run(
            c"
class StreamBridge:
    def __init__(self, sender):
        self.sender = sender
        self.closed = False
        self.mode = 'wb'
        self.name = '<rust_stream>'
        self.total_written = 0

    def write(self, data):
        if self.closed or not data:
            return 0
        if isinstance(data, str):
            data = data.encode('utf-8')
        data_bytes = bytes(data)
        if not data_bytes:
            return 0
        self.sender.send_chunk(data_bytes)
        self.total_written += len(data_bytes)
        return len(data_bytes)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def tell(self):
        return self.total_written

    def seek(self, offset, whence=0):
        return 0

    def seekable(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return not self.closed

stream_bridge = StreamBridge(rust_sender)
",
            None,
            Some(&locals),
        )
        .map_err(|e| format!("Failed to create stream bridge: {e}"))?;

        let stream_bridge = locals
            .get_item("stream_bridge")
            .map_err(|e| format!("Failed to get stream_bridge: {e}"))?
            .ok_or("stream_bridge not found")?;

        let yt_dlp = py
            .import("yt_dlp")
            .map_err(|e| format!("Failed to import yt_dlp: {e}"))?;

        let opts = PyDict::new(py);
        opts.set_item("quiet", true).unwrap();
        opts.set_item("no_warnings", true).unwrap();
        opts.set_item("noprogress", true).unwrap();
        opts.set_item("socket_timeout", 30).unwrap();
        opts.set_item("buffersize", 8192).unwrap(); // 8KB buffer - smaller for streaming
        opts.set_item("outtmpl", "-").unwrap();
        opts.set_item("logtostderr", true).unwrap();
        opts.set_item("output_stream", &stream_bridge).unwrap();
        // Force FFmpeg for HLS streaming to avoid temp file issues
        opts.set_item("external_downloader", "ffmpeg").unwrap();
        opts.set_item("external_downloader_args", vec!["-c", "copy", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov"]).unwrap();
        // Enable Deno JS runtime for yt-dlp-ejs (required for full YouTube support)
        let js_runtimes = PyDict::new(py);
        js_runtimes.set_item("deno", PyDict::new(py)).unwrap();
        opts.set_item("js_runtimes", &js_runtimes).unwrap();

        if let Some(fmt) = format_selector {
            opts.set_item("format", fmt).unwrap();
        }

        if let Some(cp) = cookies_path {
            if std::path::Path::new(cp).exists() {
                opts.set_item("cookiefile", cp).unwrap();
            }
        }

        let ydl_class = yt_dlp
            .getattr("YoutubeDL")
            .map_err(|e| format!("Failed to get YoutubeDL: {e}"))?;
        let ydl = ydl_class
            .call1((opts,))
            .map_err(|e| format!("Failed to create YoutubeDL: {e}"))?;

        eprintln!("[ytdlp] Starting download for: {}", url);
        let result = ydl.call_method1("download", ([url],));
        eprintln!("[ytdlp] Download call completed");

        let _ = ydl.call_method0("close");
        eprintln!("[ytdlp] YoutubeDL closed, total written: {:?}", 
            stream_bridge.getattr("total_written").ok().and_then(|v| v.extract::<usize>().ok()));

        match result {
            Ok(_) => {
                eprintln!("[ytdlp] Download successful");
                Ok(())
            }
            Err(e) => {
                let err_str = e.to_string();
                eprintln!("[ytdlp] Download error: {}", err_str);
                Err(classify_error(&err_str))
            }
        }
    })
}

fn classify_error(err_str: &str) -> String {
    let lower = err_str.to_lowercase();
    if lower.contains("not found") || lower.contains("unable to download") {
        format!("NOT_FOUND:{err_str}")
    } else if err_str.contains("403") || lower.contains("forbidden") {
        format!("FORBIDDEN:{err_str}")
    } else if lower.contains("login") || lower.contains("authentication") {
        format!("AUTH_REQUIRED:{err_str}")
    } else if lower.contains("unsupported url") {
        format!("UNSUPPORTED:{err_str}")
    } else {
        format!("DOWNLOAD_FAILED:{err_str}")
    }
}

/// Wrapper struct to send chunks from Python to Rust channel
#[pyclass]
struct RustSenderWrapper {
    tx: mpsc::Sender<Result<Vec<u8>, String>>,
}

#[pymethods]
impl RustSenderWrapper {
    fn send_chunk(&self, data: Vec<u8>) {
        let _ = self.tx.send(Ok(data));
    }
}

/// Stream HLS/m3u8 directly using FFmpeg subprocess.
/// This is more reliable than yt-dlp native downloader for HLS streaming.
/// FFmpeg handles segment downloading and muxing internally without temp files.
pub fn stream_hls_with_ffmpeg(
    hls_url: &str,
    http_headers: &HashMap<String, String>,
    tx: mpsc::Sender<Result<Vec<u8>, String>>,
) -> Result<(), String> {
    use std::process::{Command, Stdio};
    use std::thread;

    eprintln!("[ffmpeg] Starting HLS stream for: {}", hls_url);

    // Build ffmpeg command
    let mut cmd = Command::new("ffmpeg");
    cmd.arg("-i").arg(hls_url);
    
    // Add HTTP headers if present
    if !http_headers.is_empty() {
        let mut header_str = String::new();
        for (key, val) in http_headers {
            if key.to_lowercase() != "cookie" {
                header_str.push_str(&format!("{}: {}\r\n", key, val));
            }
        }
        if !header_str.is_empty() {
            cmd.arg("-headers").arg(header_str);
        }
        
        // Add cookies separately if present
        if let Some(cookie) = http_headers.get("Cookie").or_else(|| http_headers.get("cookie")) {
            cmd.arg("-cookies").arg(cookie);
        }
    }

    // Output options for streaming MP4
    cmd.arg("-c").arg("copy");  // Copy without re-encoding
    cmd.arg("-f").arg("mp4");   // MP4 format
    cmd.arg("-movflags").arg("frag_keyframe+empty_moov");  // Fragmented MP4 for streaming
    cmd.arg("-loglevel").arg("error");  // Reduce noise
    cmd.arg("pipe:1");  // Output to stdout
    
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("Failed to spawn ffmpeg: {}", e))?;
    
    let stdout = child.stdout.take().ok_or("Failed to capture stdout")?;
    
    // Read stdout in a separate thread and send to channel
    let reader_thread = thread::spawn(move || {
        use std::io::{BufReader, Read};
        
        let mut reader = BufReader::new(stdout);
        let mut buffer = vec![0u8; 65536];  // 64KB buffer
        
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    eprintln!("[ffmpeg] EOF reached");
                    break;
                }
                Ok(n) => {
                    let chunk = buffer[..n].to_vec();
                    if tx.send(Ok(chunk)).is_err() {
                        eprintln!("[ffmpeg] Channel closed, stopping");
                        break;
                    }
                }
                Err(e) => {
                    eprintln!("[ffmpeg] Read error: {}", e);
                    let _ = tx.send(Err(format!("Read error: {}", e)));
                    break;
                }
            }
        }
    });

    // Wait for ffmpeg to complete
    let status = child.wait().map_err(|e| format!("Failed to wait for ffmpeg: {}", e))?;
    
    // Wait for reader thread to finish
    let _ = reader_thread.join();
    
    if status.success() {
        eprintln!("[ffmpeg] Stream completed successfully");
        Ok(())
    } else {
        // Try to capture stderr
        if let Some(stderr) = child.stderr {
            use std::io::Read;
            let mut err_buf = String::new();
            let _ = stderr.take(10000).read_to_string(&mut err_buf);
            Err(format!("FFmpeg failed with exit code {:?}. Error: {}", status.code(), err_buf))
        } else {
            Err(format!("FFmpeg failed with exit code {:?}", status.code()))
        }
    }
}
