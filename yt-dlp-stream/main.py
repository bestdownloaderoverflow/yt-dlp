import asyncio
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
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

logger = logging.getLogger("uvicorn.error")

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

    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk, None
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()


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
):
    """
    Extract best audio and stream directly as m4a/webm **without ffmpeg**.
    Uses cobalt-style chunked Range requests for consistent speed regardless
    of video duration. No re-encoding — zero CPU overhead.
    """
    try:
        out_filename, direct_url, http_headers, filesize, ext = stream_generator_direct(
            url=url,
            format_str=AUDIO_FORMAT_M4A,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
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
        body = _chunked_range_generator(direct_url, safe_headers, filesize)
    else:
        async def _simple_stream() -> AsyncIterator[bytes]:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                    resp.raise_for_status()
                    async for data in resp.aiter_bytes(65536):
                        yield data
        body = _simple_stream()

    return StreamingResponse(body, media_type=media_type, headers=response_headers)


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


# -----------------------------------------------------------------------------
# Chunked video streaming (Cobalt-style) for consistent speed on long videos
# -----------------------------------------------------------------------------

async def _chunked_video_generator(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int,
    ydl_refresh_info: dict,  # For URL transplanting
) -> AsyncIterator[bytes]:
    """
    Cobalt-style chunked range download for video.
    Fetches the remote file in sequential Range requests so each chunk
    is independently retryable and the connection never times out on large files.
    Supports URL transplanting when URLs expire (403 Forbidden).
    """
    read = 0
    refresh_count = 0
    max_refreshes = 10

    safe_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        while read < total_size:
            end = min(read + chunk_size - 1, total_size - 1)
            range_headers = {**safe_headers, "Range": f"bytes={read}-{end}"}

            try:
                async with client.stream("GET", url, headers=range_headers) as resp:
                    if resp.status_code == 403 and refresh_count < max_refreshes:
                        # URL expired - try to refresh (transplant)
                        logger.warning(f"URL expired at byte {read}, attempting refresh...")
                        new_url = await _refresh_url(ydl_refresh_info)
                        if new_url:
                            url = new_url
                            refresh_count += 1
                            logger.info(f"URL refreshed successfully (attempt {refresh_count})")
                            continue  # Retry same chunk with new URL
                        else:
                            logger.error("Failed to refresh URL")
                            raise HTTPException(status_code=403, detail="URL expired and could not be refreshed")

                    resp.raise_for_status()
                    async for data in resp.aiter_bytes():
                        yield data
                        read += len(data)

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403 and refresh_count < max_refreshes:
                    logger.warning(f"HTTP 403 at byte {read}, attempting refresh...")
                    new_url = await _refresh_url(ydl_refresh_info)
                    if new_url:
                        url = new_url
                        refresh_count += 1
                        continue
                raise


async def _stream_chunked_merge(
    video_fmt: dict,
    audio_fmt: dict,
    global_headers: dict,
    ydl_opts: dict,
    original_url: str,
    format_str: str,
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

    # Queues untuk video dan audio streams
    video_queue = queue.Queue(maxsize=32)
    audio_queue = queue.Queue(maxsize=32)
    video_done = threading.Event()
    audio_done = threading.Event()

    def download_video_to_queue():
        """Download video dalam thread terpisah."""
        try:
            if video_size:
                asyncio.run(_download_chunked_to_queue(
                    video_url, video_headers, video_size,
                    VIDEO_CHUNK_SIZE, ydl_refresh_info, video_queue
                ))
            else:
                asyncio.run(_download_simple_to_queue(
                    video_url, video_headers, video_queue
                ))
        except Exception as e:
            logger.error(f"Video download error: {e}")
        finally:
            video_done.set()
            video_queue.put(None)

    def download_audio_to_queue():
        """Download audio dalam thread terpisah."""
        try:
            if audio_size:
                asyncio.run(_download_chunked_to_queue(
                    audio_url, audio_headers, audio_size,
                    VIDEO_CHUNK_SIZE, ydl_refresh_info, audio_queue
                ))
            else:
                asyncio.run(_download_simple_to_queue(
                    audio_url, audio_headers, audio_queue
                ))
        except Exception as e:
            logger.error(f"Audio download error: {e}")
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

    # Close read end in parent
    os.close(audio_r)

    # Threads untuk feed ffmpeg inputs
    def feed_video():
        try:
            while True:
                chunk = video_queue.get()
                if chunk is None:
                    break
                ffmpeg_proc.stdin.write(chunk)
            ffmpeg_proc.stdin.close()
        except Exception as e:
            logger.error(f"Video feed error: {e}")

    def feed_audio():
        try:
            while True:
                chunk = audio_queue.get()
                if chunk is None:
                    break
                os.write(audio_w, chunk)
            os.close(audio_w)
        except Exception as e:
            logger.error(f"Audio feed error: {e}")

    threading.Thread(target=feed_video, daemon=True).start()
    threading.Thread(target=feed_audio, daemon=True).start()

    # Async generator: baca dari ffmpeg stdout
    try:
        loop = asyncio.get_event_loop()
        while True:
            chunk = await loop.run_in_executor(
                None, ffmpeg_proc.stdout.read, 65536
            )
            if not chunk:
                break
            yield chunk
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()


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
    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **ydl_opts,
    }

    try:
        # Gunakan singleton manager untuk session integrity
        info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
        if not info:
            raise ValueError("Could not extract video info")

        base_filename = info.get("title", "download") or "download"
        out_filename = f"{base_filename}.mp4"

        global_headers = info.get("http_headers") or {}
        resolved = ydl_manager.resolve_formats(info, format_str, proxy=proxy, impersonate=impersonate)

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
                    body = _chunked_video_generator(
                        direct_url, http_headers, filesize, VIDEO_CHUNK_SIZE, ydl_refresh_info
                    )
                else:
                    # No filesize - use simple streaming
                    safe_headers = {k: v for k, v in http_headers.items() if k.lower() != "accept-encoding"}
                    async def _simple_stream() -> AsyncIterator[bytes]:
                        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                            async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                                resp.raise_for_status()
                                async for data in resp.aiter_bytes(65536):
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
                url, format_str
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
            return _streaming_response(
                url=url,
                format_str=format_str,
                audio_only=False,
                download=download,
                ydl_opts=ydl_opts,
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

    # Queue untuk komunikasi antara download thread dan async generator
    download_queue = queue.Queue(maxsize=32)  # Buffer ~2MB
    download_done = threading.Event()
    download_error = [None]

    def download_to_queue():
        """Download audio dalam thread terpisah, push ke queue."""
        try:
            if audio_size:
                # Use chunked download
                asyncio.run(_download_chunked_to_queue(
                    audio_url, audio_headers, audio_size,
                    CHUNK_SIZE, ydl_refresh_info, download_queue
                ))
            else:
                # Simple download
                asyncio.run(_download_simple_to_queue(
                    audio_url, audio_headers, download_queue
                ))
        except Exception as e:
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

    # Thread untuk feed ffmpeg stdin dari queue
    def feed_ffmpeg_stdin():
        try:
            while True:
                chunk = download_queue.get()
                if chunk is None:
                    break
                ffmpeg_proc.stdin.write(chunk)
            ffmpeg_proc.stdin.close()
        except Exception as e:
            logger.error(f"Feed error: {e}")

    feed_thread = threading.Thread(target=feed_ffmpeg_stdin, daemon=True)
    feed_thread.start()

    # Async generator: baca dari ffmpeg stdout dan yield ke client
    try:
        loop = asyncio.get_event_loop()
        while True:
            # Read dari stdout dalam executor agar tidak blocking
            chunk = await loop.run_in_executor(
                None, ffmpeg_proc.stdout.read, 65536
            )
            if not chunk:
                break
            yield chunk
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()


async def _download_chunked_to_queue(url, headers, size, chunk_size, refresh_info, q):
    """Helper: download chunked dan push ke queue."""
    ydl_refresh_info = refresh_info
    try:
        async for chunk in _chunked_video_generator(
            url, headers, size, chunk_size, ydl_refresh_info
        ):
            q.put(chunk)
    except Exception as e:
        logger.error(f"Chunked download error: {e}")
        raise


async def _download_simple_to_queue(url, headers, q):
    """Helper: simple download dan push ke queue."""
    safe_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url, headers=safe_headers) as resp:
            resp.raise_for_status()
            async for data in resp.aiter_bytes():
                q.put(data)


@app.get("/stream/mp3-chunked")
async def stream_mp3_chunked(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
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
    ydl_opts = _build_ydl_opts(proxy, impersonate)
    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **ydl_opts,
    }

    try:
        # Gunakan singleton manager untuk session integrity
        info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
        if not info:
            raise ValueError("Could not extract video info")

        base_filename = info.get("title", "download") or "download"
        out_filename = f"{base_filename}.mp3"

        global_headers = info.get("http_headers") or {}
        resolved = ydl_manager.resolve_formats(info, AUDIO_FORMAT, proxy=proxy, impersonate=impersonate)

        # Check if we can use chunked download or need ffmpeg
        if len(resolved) == 1 and can_use_chunked_streaming(resolved[0]):
            fmt = resolved[0]
            logger.info("Using chunked download for MP3")
            body = _stream_mp3_chunked(
                fmt, global_headers, ydl_opts, url
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
            return _streaming_response(
                url=url,
                format_str=AUDIO_FORMAT,
                audio_only=True,
                download=download,
                ydl_opts=ydl_opts,
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

from internal_tunnel import internal_tunnel


@app.get("/_internal")
async def internal_tunnel_endpoint(
    id: str = Query(..., description="Stream ID"),
    request: FastAPIRequest = None,
):
    """
    Internal tunnel endpoint - hanya accessible dari localhost.

    Digunakan oleh ffmpeg dan chunked downloader untuk akses stream
    dengan headers/cookies yang benar.
    """
    # Security: hanya allow localhost
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="Forbidden")

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
    request: FastAPIRequest = None,
):
    """
    Internal chunked download endpoint.

    Streams data dalam chunks dengan URL refresh otomatis.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="Forbidden")

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
