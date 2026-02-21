import asyncio
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional, AsyncIterator, Dict, Any
from pathlib import Path

import httpx

# Use local yt_dlp fork from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse
from urllib.parse import quote

# Import singleton manager untuk session integrity
from ytdl_manager import ydl_manager
from process_manager import process_manager
from security import is_localhost, rate_limiter, create_stream_token, validate_stream_token
from internal_tunnel import internal_tunnel

logger = logging.getLogger("ytdlp_stream")

app = FastAPI(title="yt-dlp Stream API", version="3.0.0")

# -----------------------------------------------------------------------------
# Helper functions untuk estimasi content length (seperti Cobalt)
# -----------------------------------------------------------------------------

def estimate_mp3_size(duration_seconds: Optional[int], bitrate: int = 192) -> int:
    """
    Estimasi ukuran MP3 berdasarkan durasi dan bitrate.

    Formula: (bitrate kbps * 1000 * duration) / 8 bytes
    Tambah 10% untuk metadata dan variance.

    Args:
        duration_seconds: Durasi video dalam detik
        bitrate: Bitrate MP3 dalam kbps (default 192 untuk libmp3lame)

    Returns:
        Estimated size dalam bytes, atau -1 jika duration tidak diketahui
    """
    if not duration_seconds or duration_seconds <= 0:
        return -1

    # MP3 size = (bitrate * 1000 * duration) / 8
    # Tambah 10% untuk safety margin
    estimated = int((bitrate * 1000 * duration_seconds / 8) * 1.1)
    return estimated


def estimate_video_size(filesize: Optional[int], duration: Optional[int]) -> int:
    """
    Estimasi ukuran video setelah processing.

    Untuk video yang sudah ada filesize-nya, gunakan itu.
    Untuk video tanpa filesize, estimasi berdasarkan durasi.
    """
    if filesize and filesize > 0:
        return int(filesize * 1.05)  # Tambah 5% untuk container overhead

    if duration and duration > 0:
        # Estimasi kasar: ~2MB per menit untuk 720p
        return int(duration * 2 * 1024 * 1024 / 60)

    return -1

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
AUDIO_FORMAT_M4A = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per range request (mirrors cobalt)
VIDEO_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB for video chunks


def _header_str(headers: dict) -> str:
    """Convert a headers dict to the ffmpeg -headers string format."""
    parts = []
    for k, v in headers.items():
        if k.lower() == "accept-encoding":
            continue
        parts.append(f"{k}: {v}\r\n")
    return "".join(parts)


def _client_id_from_request(request: Optional[FastAPIRequest]) -> str:
    if not request:
        return "unknown"
    client_host = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "-")
    return f"{client_host}:{ua}"


def _enforce_rate_limit(request: Optional[FastAPIRequest]) -> None:
    client_id = _client_id_from_request(request)
    if not rate_limiter.is_allowed(client_id):
        raise HTTPException(status_code=429, detail="Too many requests")


def _extract_info_and_resolve(
    url: str,
    format_str: str,
    proxy: Optional[str],
    impersonate: Optional[str],
) -> tuple[dict, list[dict], dict]:
    """Blocking yt-dlp path used via asyncio.to_thread from async endpoints."""
    info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
    if not info:
        raise ValueError("Could not extract video info")
    global_headers = info.get("http_headers") or {}
    resolved = ydl_manager.resolve_formats(
        info,
        format_str,
        proxy=proxy,
        impersonate=impersonate,
    )
    return info, resolved, global_headers


def _build_internal_chunked_stream(
    direct_url: str,
    headers: dict,
    total_size: int,
    service: str,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Create an internal stream + signed token, then stream via internal tunnel manager.
    """
    stream_id = internal_tunnel.create_stream(
        url=direct_url,
        headers=headers,
        service=service,
    )
    token = create_stream_token(stream_id)
    is_valid, error = validate_stream_token(stream_id, token.expires_at, token.signature)
    if not is_valid:
        internal_tunnel.destroy_stream(stream_id)
        raise HTTPException(status_code=500, detail=f"Failed to create internal stream token: {error}")

    async def _body() -> AsyncIterator[bytes]:
        try:
            async for chunk in internal_tunnel.read_chunks(stream_id, total_size):
                # Check for client disconnect
                if await request.is_disconnected():
                    logger.info(f"Client disconnected, destroying internal stream {stream_id}")
                    break
                yield chunk
        finally:
            internal_tunnel.destroy_stream(stream_id)

    return _body()


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
            "-b:a", "192k",
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

    NOTE: Sekarang menggunakan singleton ydl_manager untuk session integrity.
    """
    # Gunakan singleton manager untuk session integrity
    info = ydl_manager.extract_info(
        url,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

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
    resolved = ydl_manager.resolve_formats(
        info, format_str,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

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
        stderr=subprocess.DEVNULL,
    )
    
    # Register process untuk automatic cleanup
    process_manager.register_process(ffmpeg_proc, process_type="ffmpeg_stream")

    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk, None
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()
        process_manager.unregister_process(ffmpeg_proc.pid)


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


async def _streaming_response(
    url: str,
    format_str: str,
    audio_only: bool,
    download: bool,
    ydl_opts: dict,
    request: FastAPIRequest,
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

    async def chunk_generator():
        try:
            for chunk, _ in gen:
                if await request.is_disconnected():
                    logger.info("Client disconnected, stopping ffmpeg stream")
                    break
                yield chunk
        finally:
            # Force cleanup generator (closes ffmpeg proc)
            try:
                gen.close()
            except Exception:
                pass

    media_type = "audio/mpeg" if audio_only else "video/mp4"
    disposition = "attachment" if download else "inline"

    # RFC 5987/RFC 6266 encoding for non-ASCII filenames
    # filename* uses UTF-8 encoding for unicode characters
    ascii_name = filename.encode('ascii', 'ignore').decode()
    utf8_name = quote(filename, safe='')

    if ascii_name == filename:
        # All ASCII - use simple filename
        cd_header = f'{disposition}; filename="{filename.replace(chr(34), chr(92)+chr(34))}"'
    else:
        # Contains non-ASCII - use filename* for UTF-8 support
        cd_header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

    return StreamingResponse(
        chunk_generator(),
        media_type=media_type,
        headers={
            "Content-Disposition": cd_header,
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
            "video_chunked": "GET /stream/video-chunked?url=...&quality=1080 (Cobalt-style, best for long videos)",
            "video_custom": "GET /stream/video?url=...&format=<yt-dlp format string>",
            "mp3": "GET /stream/mp3?url=...",
            "mp3_chunked": "GET /stream/mp3-chunked?url=... (Cobalt-style, best for long audio)",
            "m4a": "GET /stream/m4a?url=... (no ffmpeg, chunked range, fastest)",
            "audio_legacy": "GET /stream/audio?url=...",
            "health": "GET /health (health check & metrics)",
            "stats": "GET /stats (internal statistics)",
        },
    }


@app.get("/info")
def get_info(
    url: str = Query(..., description="Video URL"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
):
    """Extract video metadata without downloading."""
    try:
        # Gunakan singleton manager untuk session integrity
        info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
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
async def stream_video(
    url: str = Query(..., description="Video URL"),
    quality: Optional[str] = Query(None, description="Quality preset: 1080, 720, 480, 360"),
    format: Optional[str] = Query(None, description="Custom yt-dlp format string (overrides quality)"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
    request: FastAPIRequest = None,
):
    """
    Stream / download video.

    - `quality`: simple preset — 1080, 720, 480, 360 (default: 1080)
    - `format`: raw yt-dlp format string (overrides quality)
    - Automatically handles DASH/HLS manifests and video-only+audio merges.
    - `impersonate`: useful for TikTok and sites with TLS fingerprinting
    """
    _enforce_rate_limit(request)

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
        return await _streaming_response(
            url=url,
            format_str=format_str,
            audio_only=False,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
            request=request,
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/mp3")
async def stream_mp3(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
    request: FastAPIRequest = None,
):
    """
    Extract best audio and stream / download as MP3 (re-encoded via libmp3lame q:2).
    """
    _enforce_rate_limit(request)

    try:
        return await _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
            request=request,
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _chunked_range_generator(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """
    Cobalt-style chunked range download: fetches the remote file in
    sequential Range requests so each chunk is independently retryable
    and the connection never times out on large files.
    """
    read = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        while read < total_size:
            end = min(read + chunk_size - 1, total_size - 1)
            range_headers = {**headers, "Range": f"bytes={read}-{end}"}
            async with client.stream("GET", url, headers=range_headers) as resp:
                resp.raise_for_status()
                async for data in resp.aiter_bytes():
                    yield data
                    read += len(data)


def stream_generator_direct(
    url: str,
    format_str: str,
    ydl_opts: dict,
):
    """
    Resolve the best audio URL via yt-dlp, then yield:
      (b"", filename, http_headers, filesize, direct_url)
    for the caller to perform a chunked range download without ffmpeg.

    NOTE: Sekarang menggunakan singleton ydl_manager untuk session integrity.
    """
    # Gunakan singleton manager untuk session integrity
    info = ydl_manager.extract_info(
        url,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

    if not info:
        raise ValueError("Could not extract video info")

    base_filename = info.get("title", "download") or "download"
    global_headers = info.get("http_headers") or {}
    resolved = ydl_manager.resolve_formats(
        info, format_str,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

    fmt = resolved[0]
    ext = fmt.get("ext") or "m4a"
    out_filename = f"{base_filename}.{ext}"
    direct_url = fmt["url"]
    http_headers = fmt.get("http_headers") or global_headers
    filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

    return out_filename, direct_url, http_headers, filesize, ext


@app.get("/stream/m4a")
async def stream_m4a(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
    request: FastAPIRequest = None,
):
    """
    Extract best audio and stream directly as m4a/webm **without ffmpeg**.
    Uses cobalt-style chunked Range requests for consistent speed regardless
    of video duration. No re-encoding — zero CPU overhead.
    """
    _enforce_rate_limit(request)

    try:
        out_filename, direct_url, http_headers, filesize, ext = await asyncio.to_thread(
            stream_generator_direct,
            url,
            AUDIO_FORMAT_M4A,
            _build_ydl_opts(proxy, impersonate),
        )
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    safe_headers = {k: v for k, v in http_headers.items() if k.lower() != "accept-encoding"}

    disposition = "attachment" if download else "inline"
    ascii_name = out_filename.encode("ascii", "ignore").decode()
    utf8_name = quote(out_filename, safe="")
    if ascii_name == out_filename:
        cd_header = f'{disposition}; filename="{out_filename.replace(chr(34), chr(92)+chr(34))}"'
    else:
        cd_header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

    ext_to_mime = {"m4a": "audio/mp4", "webm": "audio/webm", "ogg": "audio/ogg"}
    media_type = ext_to_mime.get(ext, "audio/mp4")

    response_headers = {
        "Content-Disposition": cd_header,
        "X-Accel-Buffering": "no",
    }
    if filesize:
        response_headers["Content-Length"] = str(filesize)

    if filesize:
        body = _build_internal_chunked_stream(
            direct_url=direct_url,
            headers=safe_headers,
            total_size=filesize,
            service="audio",
            request=request,
        )
    else:
        async def _simple_stream() -> AsyncIterator[bytes]:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                    resp.raise_for_status()
                    async for data in resp.aiter_bytes(65536):
                        if await request.is_disconnected():
                            logger.info("Client disconnected, stopping m4a stream")
                            break
                        yield data
        body = _simple_stream()

    return StreamingResponse(body, media_type=media_type, headers=response_headers)


@app.get("/stream/audio")
async def stream_audio(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
    request: FastAPIRequest = None,
):
    """
    Alias for /stream/mp3 — kept for backwards compatibility.
    """
    _enforce_rate_limit(request)

    try:
        return await _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
            request=request,
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Chunked video streaming (Cobalt-style) for consistent speed on long videos
# -----------------------------------------------------------------------------

async def _chunked_video_generator_with_disconnect(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int,
    ydl_refresh_info: dict,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Wrapper untuk _chunked_video_generator yang menambahkan client disconnect detection.
    Menggunakan threading.Event untuk signal cancellation ke generator.
    """
    cancel_event = threading.Event()

    async def disconnect_checker():
        """Task untuk monitor client disconnect."""
        while not cancel_event.is_set():
            if await request.is_disconnected():
                cancel_event.set()
                logger.info("Client disconnect detected, signalling cancellation")
                break
            await asyncio.sleep(0.5)  # Check every 500ms

    # Start disconnect checker task
    checker_task = asyncio.create_task(disconnect_checker())

    try:
        async for chunk in _chunked_video_generator(
            url, headers, total_size, chunk_size, ydl_refresh_info, cancel_event
        ):
            yield chunk
    finally:
        # Ensure checker task is cancelled
        cancel_event.set()
        checker_task.cancel()
        try:
            await checker_task
        except asyncio.CancelledError:
            pass


async def _chunked_video_generator(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int,
    ydl_refresh_info: dict,  # For URL transplanting
    cancel_event: threading.Event = None,  # For client disconnect cancellation
) -> AsyncIterator[bytes]:
    """
    Cobalt-style chunked range download for video.
    Fetches the remote file in sequential Range requests so each chunk
    is independently retryable and the connection never times out on large files.
    Supports URL transplanting when URLs expire (403 Forbidden).

    Improvements:
    - Per-chunk retry dengan exponential backoff
    - Better error propagation ke client
    - Detailed logging untuk debugging
    - Client disconnect detection via cancel_event
    """
    read = 0
    refresh_count = 0
    max_refreshes = 10
    max_retries_per_chunk = 3

    safe_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        while read < total_size:
            # Check for cancellation
            if cancel_event and cancel_event.is_set():
                logger.info(f"Chunked download cancelled at byte {read}/{total_size}")
                break

            end = min(read + chunk_size - 1, total_size - 1)
            range_headers = {**safe_headers, "Range": f"bytes={read}-{end}"}

            chunk_retries = 0
            chunk_success = False

            while chunk_retries < max_retries_per_chunk and not chunk_success:
                # Check for cancellation before retry
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Chunked download cancelled at byte {read}/{total_size}")
                    break

                try:
                    async with client.stream("GET", url, headers=range_headers) as resp:
                        # Handle 403 - URL expired, try transplant
                        if resp.status_code == 403:
                            if refresh_count < max_refreshes:
                                logger.warning(f"URL expired at byte {read}/{total_size}, attempting refresh {refresh_count+1}/{max_refreshes}...")
                                new_url = await _refresh_url(ydl_refresh_info)
                                if new_url:
                                    url = new_url
                                    refresh_count += 1
                                    logger.info(f"URL refreshed successfully (attempt {refresh_count})")
                                    continue  # Retry same chunk with new URL
                                else:
                                    logger.error("Failed to refresh URL")
                                    raise HTTPException(
                                        status_code=403,
                                        detail=f"URL expired at {read}/{total_size} bytes and could not be refreshed"
                                    )
                            else:
                                raise HTTPException(
                                    status_code=403,
                                    detail=f"Max refresh attempts ({max_refreshes}) exceeded at {read}/{total_size} bytes"
                                )

                        # Handle other errors
                        if resp.status_code >= 400:
                            logger.error(f"HTTP {resp.status_code} at byte {read}/{total_size}")
                            resp.raise_for_status()

                        # Success - stream chunk data
                        chunk_bytes_read = 0
                        async for data in resp.aiter_bytes():
                            # Check for cancellation during streaming
                            if cancel_event and cancel_event.is_set():
                                logger.info(f"Chunked download cancelled during streaming at byte {read}/{total_size}")
                                break
                            yield data
                            read += len(data)
                            chunk_bytes_read += len(data)

                        # If cancelled during streaming, exit outer loop
                        if cancel_event and cancel_event.is_set():
                            break

                        chunk_success = True

                        # Log progress setiap 10%
                        progress = (read / total_size) * 100
                        if int(progress) % 10 == 0:
                            logger.info(f"Download progress: {progress:.1f}% ({read}/{total_size} bytes)")

                except httpx.HTTPStatusError as e:
                    chunk_retries += 1
                    
                    # Special handling untuk 403
                    if e.response.status_code == 403 and refresh_count < max_refreshes:
                        logger.warning(f"HTTP 403 at byte {read}, attempting refresh...")
                        new_url = await _refresh_url(ydl_refresh_info)
                        if new_url:
                            url = new_url
                            refresh_count += 1
                            continue
                    
                    # Retry lain dengan backoff
                    if chunk_retries < max_retries_per_chunk:
                        backoff = min(2 ** chunk_retries, 10)  # Max 10s
                        logger.warning(f"Chunk failed (retry {chunk_retries}/{max_retries_per_chunk}), retrying in {backoff}s: {e}")
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"Chunk failed after {max_retries_per_chunk} retries at byte {read}/{total_size}: {e}")
                        raise HTTPException(
                            status_code=e.response.status_code,
                            detail=f"Download failed at {read}/{total_size} bytes after {max_retries_per_chunk} retries: {str(e)}"
                        )

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    chunk_retries += 1
                    if chunk_retries < max_retries_per_chunk:
                        backoff = min(2 ** chunk_retries, 10)
                        logger.warning(f"Network error (retry {chunk_retries}/{max_retries_per_chunk}), retrying in {backoff}s: {e}")
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"Network error after {max_retries_per_chunk} retries at byte {read}/{total_size}: {e}")
                        raise HTTPException(
                            status_code=503,
                            detail=f"Network error at {read}/{total_size} bytes after {max_retries_per_chunk} retries: {str(e)}"
                        )

                except Exception as e:
                    logger.error(f"Unexpected error at byte {read}/{total_size}: {e}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Unexpected error at {read}/{total_size} bytes: {str(e)}"
                    )
            
            if not chunk_success:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to download chunk at {read}/{total_size} bytes after all retries"
                )


async def _stream_chunked_merge(
    video_fmt: dict,
    audio_fmt: dict,
    global_headers: dict,
    ydl_opts: dict,
    original_url: str,
    format_str: str,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Stream video+audio merge dengan true streaming menggunakan named pipes.

    Video dan audio di-download secara parallel ke named pipes,
    ffmpeg membaca dari pipes sementara download berlangsung.
    Client dapat menerima data MP4 hampir seketika.
    """
    import threading
    import queue

    video_headers = video_fmt.get("http_headers") or global_headers
    audio_headers = audio_fmt.get("http_headers") or global_headers
    video_url = video_fmt["url"]
    audio_url = audio_fmt["url"]
    video_size = video_fmt.get("filesize") or video_fmt.get("filesize_approx") or 0
    audio_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0

    ydl_refresh_info = {
        "url": original_url,
        "format_str": format_str,
        "ydl_opts": ydl_opts,
    }

    # Cancellation event untuk signal disconnect
    cancel_event = threading.Event()

    # Queues untuk video dan audio streams
    video_queue = queue.Queue(maxsize=32)
    audio_queue = queue.Queue(maxsize=32)
    video_done = threading.Event()
    audio_done = threading.Event()

    def download_video_to_queue():
        """Download video dalam thread terpisah."""
        try:
            if cancel_event.is_set():
                return
            if video_size:
                asyncio.run(_download_chunked_to_queue(
                    video_url, video_headers, video_size,
                    VIDEO_CHUNK_SIZE, ydl_refresh_info, video_queue, cancel_event
                ))
            else:
                asyncio.run(_download_simple_to_queue(
                    video_url, video_headers, video_queue, cancel_event
                ))
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Video download error: {e}")
                video_queue.put(("ERROR", str(e)))
        finally:
            video_done.set()
            video_queue.put(None)

    def download_audio_to_queue():
        """Download audio dalam thread terpisah."""
        try:
            if cancel_event.is_set():
                return
            if audio_size:
                asyncio.run(_download_chunked_to_queue(
                    audio_url, audio_headers, audio_size,
                    VIDEO_CHUNK_SIZE, ydl_refresh_info, audio_queue, cancel_event
                ))
            else:
                asyncio.run(_download_simple_to_queue(
                    audio_url, audio_headers, audio_queue, cancel_event
                ))
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Audio download error: {e}")
                audio_queue.put(("ERROR", str(e)))
        finally:
            audio_done.set()
            audio_queue.put(None)

    # Start download threads
    video_thread = threading.Thread(target=download_video_to_queue, daemon=True)
    audio_thread = threading.Thread(target=download_audio_to_queue, daemon=True)
    video_thread.start()
    audio_thread.start()

    # Start ffmpeg dengan pipe untuk kedua input
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",  # Video from stdin (we'll use a thread to feed it)
        "-i", "pipe:3",  # Audio from fd 3 (we'll use another thread)
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ]

    # Create pipe for audio (fd 3)
    audio_r, audio_w = os.pipe()

    ffmpeg_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=[audio_r],
    )

    # Register process untuk automatic cleanup
    process_manager.register_process(ffmpeg_proc, process_type="ffmpeg_merge")

    # Close read end in parent
    os.close(audio_r)

    # Threads untuk feed ffmpeg inputs
    def feed_video():
        try:
            while not cancel_event.is_set():
                chunk = video_queue.get()
                if chunk is None:
                    break
                # Check for error sentinel
                if isinstance(chunk, tuple) and chunk[0] == "ERROR":
                    logger.error(f"Video download failed: {chunk[1]}")
                    ffmpeg_proc.terminate()
                    break
                ffmpeg_proc.stdin.write(chunk)
            ffmpeg_proc.stdin.close()
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Video feed error: {e}")
            try:
                ffmpeg_proc.terminate()
            except:
                pass

    def feed_audio():
        try:
            while not cancel_event.is_set():
                chunk = audio_queue.get()
                if chunk is None:
                    break
                # Check for error sentinel
                if isinstance(chunk, tuple) and chunk[0] == "ERROR":
                    logger.error(f"Audio download failed: {chunk[1]}")
                    try:
                        ffmpeg_proc.terminate()
                    except:
                        pass
                    break
                os.write(audio_w, chunk)
            os.close(audio_w)
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Audio feed error: {e}")
            try:
                ffmpeg_proc.terminate()
            except:
                pass

    threading.Thread(target=feed_video, daemon=True).start()
    threading.Thread(target=feed_audio, daemon=True).start()

    # Async generator: baca dari ffmpeg stdout dengan disconnect detection
    try:
        loop = asyncio.get_event_loop()
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping chunked merge stream")
                cancel_event.set()
                break

            chunk = await loop.run_in_executor(
                None, ffmpeg_proc.stdout.read, 65536
            )
            if not chunk:
                break
            yield chunk
    finally:
        # Signal cancellation ke semua threads
        cancel_event.set()

        # Cleanup ffmpeg
        try:
            ffmpeg_proc.stdout.close()
            ffmpeg_proc.terminate()
            ffmpeg_proc.wait(timeout=2)
        except:
            try:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait(timeout=1)
            except:
                pass
        process_manager.unregister_process(ffmpeg_proc.pid)

        # Cleanup audio pipe
        try:
            os.close(audio_w)
        except:
            pass

        # Drain queues untuk unblock threads
        try:
            while not video_queue.empty():
                video_queue.get_nowait()
        except:
            pass
        try:
            while not audio_queue.empty():
                audio_queue.get_nowait()
        except:
            pass


async def _refresh_url(ydl_refresh_info: dict) -> Optional[str]:
    """
    Refresh expired URL by re-extracting info from yt-dlp.
    This is the 'transplant' mechanism from Cobalt.

    NOTE: Sekarang menggunakan singleton ydl_manager untuk session integrity.
    """
    try:
        import asyncio
        loop = asyncio.get_event_loop()

        def _extract():
            # Gunakan singleton manager untuk session integrity
            info = ydl_manager.extract_info(
                ydl_refresh_info["url"],
                proxy=ydl_refresh_info["ydl_opts"].get("proxy"),
                impersonate=ydl_refresh_info["ydl_opts"].get("impersonate")
            )
            if not info:
                return None
            resolved = ydl_manager.resolve_formats(
                info, ydl_refresh_info["format_str"],
                proxy=ydl_refresh_info["ydl_opts"].get("proxy"),
                impersonate=ydl_refresh_info["ydl_opts"].get("impersonate")
            )
            fmt = resolved[0]
            return fmt["url"]

        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        logger.error(f"URL refresh failed: {e}")
        return None


def can_use_chunked_streaming(fmt: dict) -> bool:
    """
    Check if format supports chunked HTTP range requests.
    HLS/DASH manifests should use ffmpeg, not chunked download.
    """
    protocol = fmt.get("protocol", "")
    # Only plain HTTP/HTTPS progressive download supports range requests reliably
    return protocol in ("http", "https", "")


def can_use_chunked_streaming_multi(formats: list[dict]) -> bool:
    """
    Check if all formats support chunked HTTP range requests.
    Returns True only if all formats are plain HTTP progressive download.
    """
    return all(can_use_chunked_streaming(fmt) for fmt in formats)


@app.get("/stream/video-chunked")
async def stream_video_chunked(
    url: str = Query(..., description="Video URL"),
    quality: Optional[str] = Query(None, description="Quality preset: 1080, 720, 480, 360"),
    format: Optional[str] = Query(None, description="Custom yt-dlp format string (overrides quality)"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
    request: FastAPIRequest = None,
):
    """
    Stream video using Cobalt-style chunked range requests.

    This endpoint is optimized for long videos - it downloads in 10MB chunks
    with fresh connections per chunk, avoiding the speed degradation that
    occurs with single long-running connections.

    - Falls back to ffmpeg streaming for HLS/DASH manifests
    - Supports URL transplanting (refresh) when URLs expire mid-download
    - Best for: YouTube progressive download formats, long videos

    Query params:
    - `quality`: 1080, 720, 480, 360 (default: 1080)
    - `format`: raw yt-dlp format string (overrides quality)
    - `download`: force attachment disposition
    - `impersonate`: browser for TLS fingerprinting
    """
    _enforce_rate_limit(request)

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

    ydl_opts = _build_ydl_opts(proxy, impersonate)
    try:
        info, resolved, global_headers = await asyncio.to_thread(
            _extract_info_and_resolve,
            url,
            format_str,
            proxy,
            impersonate,
        )

        base_filename = info.get("title", "download") or "download"
        out_filename = f"{base_filename}.mp4"

        # Check if we can use chunked download or need ffmpeg
        can_chunk = len(resolved) == 1 and can_use_chunked_streaming(resolved[0])
        can_chunk_multi = len(resolved) == 2 and can_use_chunked_streaming_multi(resolved)

        if can_chunk:
                # Single progressive download file
                fmt = resolved[0]
                direct_url = fmt["url"]
                http_headers = fmt.get("http_headers") or global_headers
                filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

                if filesize:
                    # Use chunked streaming with URL transplant support
                    ydl_refresh_info = {
                        "url": url,
                        "format_str": format_str,
                        "ydl_opts": ydl_opts,
                    }
                    body = _chunked_video_generator_with_disconnect(
                        direct_url, http_headers, filesize, VIDEO_CHUNK_SIZE, ydl_refresh_info, request
                    )
                else:
                    # No filesize - use simple streaming with disconnect detection
                    safe_headers = {k: v for k, v in http_headers.items() if k.lower() != "accept-encoding"}
                    async def _simple_stream() -> AsyncIterator[bytes]:
                        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                            async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                                resp.raise_for_status()
                                async for data in resp.aiter_bytes(65536):
                                    if await request.is_disconnected():
                                        logger.info("Client disconnected, stopping simple video stream")
                                        break
                                    yield data
                    body = _simple_stream()

                # Build response headers dengan Content-Length atau estimasi
                disposition = "attachment" if download else "inline"
                ascii_name = out_filename.encode("ascii", "ignore").decode()
                utf8_name = quote(out_filename, safe="")
                if ascii_name == out_filename:
                    cd_header = f'{disposition}; filename="{out_filename.replace(chr(34), chr(92)+chr(34))}"'
                else:
                    cd_header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

                response_headers = {
                    "Content-Disposition": cd_header,
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                }

                # Gunakan Content-Length exact jika tersedia, atau estimasi jika tidak
                if filesize:
                    response_headers["Content-Length"] = str(filesize)
                else:
                    # Estimasi berdasarkan durasi untuk progress bar browser
                    duration = info.get("duration")
                    estimated_size = estimate_video_size(None, duration)
                    if estimated_size > 0:
                        response_headers["Estimated-Content-Length"] = str(estimated_size)
                        logger.info(f"Estimated video size: {estimated_size} bytes for duration {duration}s")

                return StreamingResponse(
                    body,
                    media_type="video/mp4",
                    headers=response_headers,
                )

        elif can_chunk_multi:
            # Separate video + audio, both progressive download
            # Download both to temp files with chunked method, then merge with ffmpeg
            video_fmt, audio_fmt = resolved[0], resolved[1]
            if video_fmt.get("vcodec", "none") in (None, "none", ""):
                video_fmt, audio_fmt = audio_fmt, video_fmt

            logger.info("Using chunked download for separate video+audio tracks")
            body = _stream_chunked_merge(
                video_fmt, audio_fmt, global_headers,
                {"proxy": proxy, "impersonate": impersonate},
                url, format_str, request
            )

            # Build response headers dengan estimasi content length
            disposition = "attachment" if download else "inline"
            ascii_name = out_filename.encode("ascii", "ignore").decode()
            utf8_name = quote(out_filename, safe="")
            if ascii_name == out_filename:
                cd_header = f'{disposition}; filename="{out_filename.replace(chr(34), chr(92)+chr(34))}"'
            else:
                cd_header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

            # Estimasi ukuran total dari video + audio
            video_size = video_fmt.get("filesize") or video_fmt.get("filesize_approx") or 0
            audio_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0
            total_size = video_size + audio_size

            response_headers = {
                "Content-Disposition": cd_header,
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            }

            if total_size > 0:
                # Tambah 5% untuk muxing overhead
                estimated_size = int(total_size * 1.05)
                response_headers["Estimated-Content-Length"] = str(estimated_size)
                logger.info(f"Estimated merged video size: {estimated_size} bytes")
            else:
                # Fallback ke estimasi berdasarkan durasi
                duration = info.get("duration")
                estimated_size = estimate_video_size(None, duration)
                if estimated_size > 0:
                    response_headers["Estimated-Content-Length"] = str(estimated_size)

            return StreamingResponse(
                body,
                media_type="video/mp4",
                headers=response_headers,
            )

        else:
            # HLS/DASH - use ffmpeg
            logger.info("Format requires ffmpeg streaming (HLS/DASH)")
            return await _streaming_response(
                url=url,
                format_str=format_str,
                audio_only=False,
                download=download,
                ydl_opts=ydl_opts,
                request=request,
            )

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error in stream_video_chunked")
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Chunked MP3 streaming (Cobalt-style) for consistent speed on long audio
# -----------------------------------------------------------------------------

async def _stream_mp3_chunked(
    audio_fmt: dict,
    global_headers: dict,
    ydl_opts: dict,
    original_url: str,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Stream MP3 dengan true streaming menggunakan pipe.

    FFmpeg membaca dari stdin sementara download berlangsung,
    jadi client dapat menerima data MP3 hampir seketika.

    Key: Gunakan thread untuk download dan pipe ke ffmpeg stdin,
    sementara asyncio membaca stdout ffmpeg dan yield ke client.
    """
    import threading
    import queue

    audio_headers = audio_fmt.get("http_headers") or global_headers
    audio_url = audio_fmt["url"]
    audio_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0

    ydl_refresh_info = {
        "url": original_url,
        "format_str": AUDIO_FORMAT,
        "ydl_opts": ydl_opts,
    }

    # Cancellation event untuk signal disconnect
    cancel_event = threading.Event()

    # Queue untuk komunikasi antara download thread dan async generator
    download_queue = queue.Queue(maxsize=32)  # Buffer ~2MB
    download_done = threading.Event()
    download_error = [None]

    def download_to_queue():
        """Download audio dalam thread terpisah, push ke queue."""
        try:
            if cancel_event.is_set():
                return
            if audio_size:
                # Use chunked download
                asyncio.run(_download_chunked_to_queue(
                    audio_url, audio_headers, audio_size,
                    CHUNK_SIZE, ydl_refresh_info, download_queue, cancel_event
                ))
            else:
                # Simple download
                asyncio.run(_download_simple_to_queue(
                    audio_url, audio_headers, download_queue, cancel_event
                ))
        except Exception as e:
            if not cancel_event.is_set():
                download_error[0] = e
                logger.error(f"Download error: {e}")
        finally:
            download_done.set()
            download_queue.put(None)  # Signal EOF

    # Start download thread
    download_thread = threading.Thread(target=download_to_queue, daemon=True)
    download_thread.start()

    # Start ffmpeg dengan pipe stdin
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",  # Read from stdin
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        "-f", "mp3",
        "pipe:1",
    ]

    ffmpeg_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    # Register process untuk automatic cleanup
    process_manager.register_process(ffmpeg_proc, process_type="ffmpeg_audio")

    # Thread untuk feed ffmpeg stdin dari queue
    def feed_ffmpeg_stdin():
        try:
            while not cancel_event.is_set():
                chunk = download_queue.get()
                if chunk is None:
                    break
                ffmpeg_proc.stdin.write(chunk)
            ffmpeg_proc.stdin.close()
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Feed error: {e}")

    feed_thread = threading.Thread(target=feed_ffmpeg_stdin, daemon=True)
    feed_thread.start()

    # Async generator: baca dari ffmpeg stdout dan yield ke client dengan disconnect detection
    try:
        loop = asyncio.get_event_loop()
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping MP3 stream")
                cancel_event.set()
                break

            # Read dari stdout dalam executor agar tidak blocking
            chunk = await loop.run_in_executor(
                None, ffmpeg_proc.stdout.read, 65536
            )
            if not chunk:
                break
            yield chunk
    finally:
        # Signal cancellation ke semua threads
        cancel_event.set()

        # Cleanup ffmpeg
        try:
            ffmpeg_proc.stdout.close()
            ffmpeg_proc.terminate()
            ffmpeg_proc.wait(timeout=2)
        except:
            try:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait(timeout=1)
            except:
                pass
        process_manager.unregister_process(ffmpeg_proc.pid)

        # Drain queue untuk unblock threads
        try:
            while not download_queue.empty():
                download_queue.get_nowait()
        except:
            pass


async def _download_chunked_to_queue(url, headers, size, chunk_size, refresh_info, q, cancel_event=None):
    """Helper: download chunked dan push ke queue."""
    ydl_refresh_info = refresh_info
    try:
        async for chunk in _chunked_video_generator(
            url, headers, size, chunk_size, ydl_refresh_info, cancel_event
        ):
            if cancel_event and cancel_event.is_set():
                break
            q.put(chunk)
    except Exception as e:
        if cancel_event is None or not cancel_event.is_set():
            logger.error(f"Chunked download error: {e}")
        raise


async def _download_simple_to_queue(url, headers, q, cancel_event=None):
    """Helper: simple download dan push ke queue."""
    safe_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url, headers=safe_headers) as resp:
            resp.raise_for_status()
            async for data in resp.aiter_bytes():
                if cancel_event and cancel_event.is_set():
                    break
                q.put(data)


@app.get("/stream/mp3-chunked")
async def stream_mp3_chunked(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
    request: FastAPIRequest = None,
):
    """
    Stream MP3 using Cobalt-style chunked range requests.

    This endpoint is optimized for long audio - it downloads in 8MB chunks
    with fresh connections per chunk, avoiding the speed degradation that
    occurs with single long-running connections. Audio is re-encoded to MP3
    via libmp3lame at 192k.

    - Falls back to ffmpeg streaming for HLS/DASH manifests
    - Supports URL transplanting (refresh) when URLs expire mid-download
    - Best for: Long audio tracks, podcasts, music

    Query params:
    - `download`: force attachment disposition
    - `impersonate`: browser for TLS fingerprinting
    """
    _enforce_rate_limit(request)

    ydl_opts = _build_ydl_opts(proxy, impersonate)

    try:
        info, resolved, global_headers = await asyncio.to_thread(
            _extract_info_and_resolve,
            url,
            AUDIO_FORMAT,
            proxy,
            impersonate,
        )

        base_filename = info.get("title", "download") or "download"
        out_filename = f"{base_filename}.mp3"

        # Check if we can use chunked download or need ffmpeg
        if len(resolved) == 1 and can_use_chunked_streaming(resolved[0]):
            fmt = resolved[0]
            logger.info("Using chunked download for MP3")
            body = _stream_mp3_chunked(
                fmt, global_headers, ydl_opts, url, request
            )

            # Build response headers dengan estimasi content length
            # (seperti Cobalt) untuk progress bar di browser
            disposition = "attachment" if download else "inline"
            ascii_name = out_filename.encode("ascii", "ignore").decode()
            utf8_name = quote(out_filename, safe="")
            if ascii_name == out_filename:
                cd_header = f'{disposition}; filename="{out_filename.replace(chr(34), chr(92)+chr(34))}"'
            else:
                cd_header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

            # Estimasi ukuran MP3 untuk progress bar browser
            duration = info.get("duration")
            estimated_size = estimate_mp3_size(duration)

            response_headers = {
                "Content-Disposition": cd_header,
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            }

            # Tambah estimasi content length jika duration diketahui
            if estimated_size > 0:
                response_headers["Estimated-Content-Length"] = str(estimated_size)
                logger.info(f"Estimated MP3 size: {estimated_size} bytes for duration {duration}s")

            return StreamingResponse(
                body,
                media_type="audio/mpeg",
                headers=response_headers,
            )
        else:
            # HLS/DASH - use ffmpeg streaming
            logger.info("Format requires ffmpeg streaming (HLS/DASH)")
            return await _streaming_response(
                url=url,
                format_str=AUDIO_FORMAT,
                audio_only=True,
                download=download,
                ydl_opts=ydl_opts,
                request=request,
            )

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error in stream_mp3_chunked")
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Internal Tunnel Endpoints (Two-tier tunnel system)
# -----------------------------------------------------------------------------


@app.get("/_internal")
async def internal_tunnel_endpoint(
    id: str = Query(..., description="Stream ID"),
    expires: float = Query(..., description="Token expiration timestamp"),
    sig: str = Query(..., description="HMAC signature"),
    request: FastAPIRequest = None,
):
    """
    Internal tunnel endpoint - hanya accessible dari localhost dengan valid signature.

    Digunakan oleh ffmpeg dan chunked downloader untuk akses stream
    dengan headers/cookies yang benar.
    
    Security:
    - Localhost only
    - HMAC signature validation
    - Token expiration check
    """
    # Security: hanya allow localhost
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        logger.warning(f"Forbidden access to internal endpoint from {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # Validate signature & expiration
    is_valid, error = validate_stream_token(id, expires, sig)
    if not is_valid:
        logger.warning(f"Invalid stream token for {id}: {error}")
        raise HTTPException(status_code=401, detail=error)

    stream = internal_tunnel.get_stream(id)
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Return stream info untuk ffmpeg/internal use
    return {
        "url": stream.url,
        "headers": stream.headers,
        "service": stream.service,
    }


@app.get("/_internal/chunked")
async def internal_chunked_endpoint(
    id: str = Query(..., description="Stream ID"),
    size: int = Query(..., description="Total file size"),
    expires: float = Query(..., description="Token expiration timestamp"),
    sig: str = Query(..., description="HMAC signature"),
    request: FastAPIRequest = None,
):
    """
    Internal chunked download endpoint dengan security.

    Streams data dalam chunks dengan URL refresh otomatis.
    
    Security:
    - Localhost only
    - HMAC signature validation
    - Token expiration check
    """
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        logger.warning(f"Forbidden access to internal chunked endpoint from {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # Validate signature & expiration
    is_valid, error = validate_stream_token(id, expires, sig)
    if not is_valid:
        logger.warning(f"Invalid stream token for chunked {id}: {error}")
        raise HTTPException(status_code=401, detail=error)

    async def chunk_generator():
        try:
            async for chunk in internal_tunnel.read_chunks(id, size):
                yield chunk
        finally:
            # Cleanup setelah selesai
            internal_tunnel.destroy_stream(id)

    return StreamingResponse(
        chunk_generator(), media_type="application/octet-stream"
    )


# -----------------------------------------------------------------------------
# Health Check & Monitoring Endpoints
# -----------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """
    Health check endpoint untuk monitoring.
    
    Returns:
    - System health status
    - Active processes count
    - YoutubeDL manager stats
    - Memory/CPU usage
    """
    import psutil
    
    process = psutil.Process()
    memory_info = process.memory_info()
    
    return {
        "status": "healthy",
        "version": "3.0.0",
        "uptime_seconds": int(time.time() - process.create_time()),
        "system": {
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory_mb": memory_info.rss / 1024 / 1024,
            "threads": process.num_threads(),
        },
        "managers": {
            "ytdl": ydl_manager.stats,
            "processes": process_manager.stats,
        },
    }


@app.get("/stats")
async def get_stats(
    request: FastAPIRequest = None,
):
    """
    Internal statistics endpoint.
    
    Security: Hanya accessible dari localhost.
    """
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    import psutil
    
    process = psutil.Process()
    
    return {
        "ytdl_manager": ydl_manager.stats,
        "process_manager": process_manager.stats,
        "system": {
            "pid": process.pid,
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory": {
                "rss_mb": process.memory_info().rss / 1024 / 1024,
                "vms_mb": process.memory_info().vms / 1024 / 1024,
            },
            "threads": process.num_threads(),
            "open_files": len(process.open_files()),
            "connections": len(process.connections()),
        },
    }


@app.post("/admin/cleanup")
async def admin_cleanup(
    request: FastAPIRequest = None,
):
    """
    Force cleanup endpoint untuk maintenance.
    
    Security: Hanya accessible dari localhost.
    """
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # Cleanup YoutubeDL instances
    ydl_manager.force_cleanup()
    
    # Cleanup processes (will terminate stuck processes)
    process_manager._cleanup_old_processes()
    process_manager._cleanup_dead_processes()
    
    return {
        "status": "cleanup completed",
        "remaining_processes": len(process_manager._processes),
    }
