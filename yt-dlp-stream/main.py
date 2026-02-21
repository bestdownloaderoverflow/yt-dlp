import logging
import subprocess
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="yt-dlp Stream API", version="3.0.0")

# ---------------------------------------------------------------------------
# Quality preset format strings
# ---------------------------------------------------------------------------
QUALITY_FORMATS = {
    "1080": (
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=1080]+bestaudio"
        "/best[height<=1080]"
        "/best"
    ),
    "720": (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=720]+bestaudio"
        "/best[height<=720]"
        "/best"
    ),
    "480": (
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=480]+bestaudio"
        "/best[height<=480]"
        "/best"
    ),
    "360": (
        "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=360]+bestaudio"
        "/best[height<=360]"
        "/best"
    ),
}

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"


def _header_str(headers: dict) -> str:
    """Convert a headers dict to the ffmpeg -headers string format."""
    parts = []
    for k, v in headers.items():
        if k.lower() == "accept-encoding":
            continue
        parts.append(f"{k}: {v}\r\n")
    return "".join(parts)


def resolve_formats(info: dict, format_str: str, ydl: yt_dlp.YoutubeDL) -> list[dict]:
    """
    Use yt-dlp's format selector to resolve the format string against the
    extracted info dict.  Returns a list of format dicts:
      - 1 item  → single muxed stream (or manifest URL)
      - 2 items → video-only + audio-only that must be merged by ffmpeg

    Reuses the existing YoutubeDL instance so proxy, impersonate, and all
    request-level settings are guaranteed identical to the extract_info call.
    build_format_selector is purely local (no HTTP requests).
    """
    formats = info.get("formats", [])
    if not formats:
        if info.get("url"):
            return [info]
        raise ValueError("No formats or URL found in info")

    selector = ydl.build_format_selector(format_str)
    selected = list(selector(info))

    if not selected:
        raise ValueError(f"No format matching '{format_str}' found")

    top = selected[0]
    if "requested_formats" in top:
        return list(top["requested_formats"])

    return [top]


def _is_hls(fmt: dict) -> bool:
    return fmt.get("protocol", "") in ("m3u8", "m3u8_native")


def _is_dash(fmt: dict) -> bool:
    return fmt.get("protocol", "") in ("http_dash_segments",)


def build_ffmpeg_merge_cmd(
    video_fmt: dict,
    audio_fmt: dict,
    global_headers: dict,
) -> list[str]:
    """
    Build an ffmpeg command that reads a separate video stream and audio stream
    and muxes them into fragmented MP4 on stdout.
    Handles plain HTTP, HLS (.m3u8) and DASH (.mpd) manifests transparently.

    Per-format http_headers from yt-dlp (already computed by _calc_headers)
    take priority over global_headers.

    For HLS: ffmpeg applies -headers to ALL sub-requests (manifest, segments,
    key URIs) automatically, matching yt-dlp's FFmpegFD behaviour.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    for fmt in (video_fmt, audio_fmt):
        headers = fmt.get("http_headers") or global_headers
        hdr = _header_str(headers)
        if hdr:
            cmd.extend(["-headers", hdr])
        cmd.extend(["-i", fmt["url"]])

    # -c:a aac re-encodes audio; needed when merging HLS/DASH streams where
    # audio may be in ADTS/raw AAC that cannot be directly muxed into MP4.
    # Mirror of yt-dlp FFmpegFD which also uses -c:a aac for merge cases.
    cmd.extend([
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ])
    return cmd


def build_ffmpeg_single_cmd(
    fmt: dict,
    global_headers: dict,
    audio_only: bool,
) -> list[str]:
    """
    Build an ffmpeg command for a single-stream input (muxed video+audio,
    HLS/DASH manifest, or audio-only).  Encodes to MP3 when audio_only=True,
    otherwise remuxes to fragmented MP4.

    Per-format http_headers from yt-dlp (already computed by _calc_headers)
    take priority over global_headers.

    For HLS/DASH: ffmpeg applies -headers to ALL sub-requests automatically
    (manifest fetch, every segment, AES-128 key URI), matching yt-dlp's
    FFmpegFD behaviour where headers are set once per input.

    For HLS→MP4: adds -bsf:a aac_adtstoasc to fix AAC ADTS→MP4 muxing,
    mirroring yt-dlp FFmpegFD (external.py line ~609).
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    headers = fmt.get("http_headers") or global_headers
    hdr = _header_str(headers)
    if hdr:
        cmd.extend(["-headers", hdr])

    cmd.extend(["-i", fmt["url"]])

    if audio_only:
        cmd.extend([
            "-vn",
            "-acodec", "libmp3lame",
            "-q:a", "2",
            "-f", "mp3",
            "pipe:1",
        ])
    else:
        out_args = [
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+faststart",
            "-f", "mp4",
        ]
        # HLS streams with AAC audio need ADTS-to-ASC bitstream filter
        # when muxing into MP4, otherwise audio is unplayable.
        # Mirrors yt-dlp FFmpegFD behaviour (downloader/external.py).
        acodec = fmt.get("acodec", "") or ""
        if _is_hls(fmt) and acodec.split(".")[0] in ("aac", "mp4a", ""):
            out_args = ["-c", "copy", "-bsf:a", "aac_adtstoasc",
                        "-movflags", "frag_keyframe+empty_moov+faststart",
                        "-f", "mp4"]
        cmd.extend(out_args)
        cmd.append("pipe:1")

    return cmd


def stream_generator_ffmpeg(
    url: str,
    format_str: str,
    audio_only: bool,
    ydl_opts: dict,
    chunk_size: int = 65536,
):
    """
    Pipeline:
    1. yt-dlp extracts video info (with full http_headers computed per-format
       via _calc_headers, including User-Agent, Referer, etc.)
    2. Detect strategy:
       - audio_only          → single stream, encode to MP3
       - 2 resolved formats  → video-only + audio-only, merge via ffmpeg
       - 1 resolved format   → single muxed stream or HLS/DASH manifest
    3. ffmpeg downloads / demuxes / remuxes and pipes to stdout
    4. Yield (b"", filename) first, then (chunk, None) for each data chunk
    """
    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **ydl_opts,
    }

    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("Could not extract video info")

        base_filename = info.get("title", "download") or "download"
        ext = "mp3" if audio_only else "mp4"
        out_filename = f"{base_filename}.{ext}"

        yield b"", out_filename

        # global_headers: fallback if a format has no per-format http_headers.
        # yt-dlp already strips Cookie from here (security) and injects
        # per-format headers (User-Agent, Referer, etc.) via _calc_headers.
        global_headers = info.get("http_headers") or {}
        resolved = resolve_formats(info, format_str, ydl)

    if audio_only:
        ffmpeg_cmd = build_ffmpeg_single_cmd(
            fmt=resolved[0],
            global_headers=global_headers,
            audio_only=True,
        )
    elif len(resolved) == 2:
        video_fmt, audio_fmt = resolved[0], resolved[1]
        if video_fmt.get("vcodec", "none") in (None, "none", ""):
            video_fmt, audio_fmt = audio_fmt, video_fmt
        ffmpeg_cmd = build_ffmpeg_merge_cmd(
            video_fmt=video_fmt,
            audio_fmt=audio_fmt,
            global_headers=global_headers,
        )
    else:
        ffmpeg_cmd = build_ffmpeg_single_cmd(
            fmt=resolved[0],
            global_headers=global_headers,
            audio_only=False,
        )

    logger.debug(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk, None
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()

        if ffmpeg_proc.returncode not in (0, None):
            stderr_out = ffmpeg_proc.stderr.read().decode(errors="replace")
            logger.error(f"FFmpeg exited {ffmpeg_proc.returncode}: {stderr_out}")
        ffmpeg_proc.stderr.close()


def _build_ydl_opts(
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
) -> dict:
    """
    Build yt-dlp options dict with request-level settings.
    impersonate: enables TLS fingerprinting (e.g. 'chrome', 'safari') — required for TikTok.
    proxy: optional proxy URL.
    """
    opts = {}
    if proxy:
        opts["proxy"] = proxy
    if impersonate:
        opts["impersonate"] = impersonate
    return opts


def _streaming_response(
    url: str,
    format_str: str,
    audio_only: bool,
    download: bool,
    ydl_opts: dict,
) -> StreamingResponse:
    gen = stream_generator_ffmpeg(
        url=url,
        format_str=format_str,
        audio_only=audio_only,
        ydl_opts=ydl_opts,
    )

    try:
        _, filename = next(gen)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    def chunk_generator():
        for chunk, _ in gen:
            yield chunk

    media_type = "audio/mpeg" if audio_only else "video/mp4"
    disposition = "attachment" if download else "inline"
    safe_name = filename.replace('"', '\\"')

    return StreamingResponse(
        chunk_generator(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
def root():
    return {
        "message": "yt-dlp Stream API",
        "version": "3.0.0",
        "docs": "/docs",
        "endpoints": {
            "info": "GET /info?url=...",
            "video": "GET /stream/video?url=...&quality=1080|720|480|360",
            "video_custom": "GET /stream/video?url=...&format=<yt-dlp format string>",
            "mp3": "GET /stream/mp3?url=...",
            "audio_legacy": "GET /stream/audio?url=...",
        },
    }


@app.get("/info")
def get_info(
    url: str = Query(..., description="Video URL"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
):
    """Extract video metadata without downloading."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **_build_ydl_opts(proxy, impersonate),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "id": info.get("id"),
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
                "formats": [
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution"),
                        "height": f.get("height"),
                        "width": f.get("width"),
                        "filesize": f.get("filesize"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "tbr": f.get("tbr"),
                        "protocol": f.get("protocol"),
                    }
                    for f in info.get("formats", [])
                ],
            }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/video")
def stream_video(
    url: str = Query(..., description="Video URL"),
    quality: Optional[str] = Query(None, description="Quality preset: 1080, 720, 480, 360"),
    format: Optional[str] = Query(None, description="Custom yt-dlp format string (overrides quality)"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
):
    """
    Stream / download video.

    - `quality`: simple preset — 1080, 720, 480, 360 (default: 1080)
    - `format`: raw yt-dlp format string (overrides quality)
    - Automatically handles DASH/HLS manifests and video-only+audio merges.
    - `impersonate`: useful for TikTok and sites with TLS fingerprinting
    """
    if format:
        format_str = format
    elif quality:
        if quality not in QUALITY_FORMATS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid quality '{quality}'. Choose from: {', '.join(QUALITY_FORMATS)}",
            )
        format_str = QUALITY_FORMATS[quality]
    else:
        format_str = QUALITY_FORMATS["1080"]

    try:
        return _streaming_response(
            url=url,
            format_str=format_str,
            audio_only=False,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/mp3")
def stream_mp3(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
):
    """
    Extract best audio and stream / download as MP3 (re-encoded via libmp3lame q:2).
    """
    try:
        return _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/audio")
def stream_audio(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
):
    """
    Alias for /stream/mp3 — kept for backwards compatibility.
    """
    try:
        return _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
