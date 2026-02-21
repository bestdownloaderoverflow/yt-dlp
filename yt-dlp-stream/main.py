import logging
import os
import subprocess
import tempfile
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


def resolve_formats(info: dict, format_str: str) -> list[dict]:
    """
    Use yt-dlp's format selector to resolve the format string against the
    extracted info dict.  Returns a list of format dicts:
      - 1 item  → single muxed stream (or manifest URL)
      - 2 items → video-only + audio-only that must be merged by ffmpeg
    """
    formats = info.get("formats", [])
    if not formats:
        if info.get("url"):
            return [info]
        raise ValueError("No formats or URL found in info")

    with yt_dlp.YoutubeDL({"format": format_str}) as ydl:
        selector = ydl.build_format_selector(format_str)
        selected = list(selector(info))

    if not selected:
        raise ValueError(f"No format matching '{format_str}' found")

    top = selected[0]
    if "requested_formats" in top:
        return list(top["requested_formats"])

    return [top]


def build_ffmpeg_merge_cmd(
    video_fmt: dict,
    audio_fmt: dict,
    global_headers: dict,
    cookies_tempfile: Optional[str],
) -> list[str]:
    """
    Build an ffmpeg command that reads a separate video stream and audio stream
    and muxes them into fragmented MP4 on stdout.
    Handles plain HTTP, HLS (.m3u8) and DASH (.mpd) manifests transparently.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    for fmt in (video_fmt, audio_fmt):
        headers = fmt.get("http_headers", global_headers)
        hdr = _header_str(headers)
        if hdr:
            cmd.extend(["-headers", hdr])
        if cookies_tempfile:
            cmd.extend(["-cookies", cookies_tempfile])
        cmd.extend(["-i", fmt["url"]])

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
    cookies_tempfile: Optional[str] = None,
) -> list[str]:
    """
    Build an ffmpeg command for a single-stream input (muxed video+audio,
    HLS/DASH manifest, or audio-only).  Encodes to MP3 when audio_only=True,
    otherwise remuxes to fragmented MP4.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    headers = fmt.get("http_headers", global_headers)
    hdr = _header_str(headers)
    if hdr:
        cmd.extend(["-headers", hdr])
    if cookies_tempfile:
        cmd.extend(["-cookies", cookies_tempfile])

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
        cmd.extend([
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+faststart",
            "-f", "mp4",
            "pipe:1",
        ])

    return cmd


def write_cookies_to_netscape_format(cookiejar, temp_dir: str) -> Optional[str]:
    """
    Write cookies from cookiejar to a temporary file in Netscape format for ffmpeg.
    Returns the path to the temp file or None if no cookies.
    """
    if not cookiejar or len(cookiejar) == 0:
        return None

    # Create temp file for cookies
    fd, cookies_path = tempfile.mkstemp(suffix=".txt", dir=temp_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# This file was generated by yt-dlp-stream\n\n")

            for cookie in cookiejar:
                domain = cookie.domain
                # Netscape format: domain must start with dot for subdomains
                include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
                if not domain.startswith(".") and cookie.domain_specified:
                    domain = "." + domain

                secure = "TRUE" if cookie.secure else "FALSE"
                path = cookie.path or "/"
                expires = str(int(cookie.expires)) if cookie.expires else "0"
                name = cookie.name
                value = cookie.value or ""

                f.write("\t".join([
                    domain,
                    include_subdomains,
                    path,
                    secure,
                    expires,
                    name,
                    value,
                ]) + "\n")

        return cookies_path
    except Exception as e:
        logger.error(f"Failed to write cookies: {e}")
        try:
            os.unlink(cookies_path)
        except:
            pass
        return None


def stream_generator_ffmpeg(
    url: str,
    format_str: str,
    audio_only: bool,
    ydl_opts: dict,
    chunk_size: int = 65536,
):
    """
    Pipeline:
    1. yt-dlp extracts video info and resolves format(s)
    2. Detect strategy:
       - audio_only          → single stream, encode to MP3
       - 2 resolved formats  → video-only + audio-only, merge via ffmpeg
       - 1 resolved format   → single muxed stream or HLS/DASH manifest
    3. ffmpeg downloads / demuxes / remuxes and pipes to stdout
    4. Yield (b"", filename) first, then (chunk, None) for each data chunk
    """
    temp_dir = tempfile.mkdtemp()
    cookies_tempfile = None

    try:
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

            global_headers = info.get("http_headers", {})
            cookies_tempfile = write_cookies_to_netscape_format(ydl.cookiejar, temp_dir)
            resolved = resolve_formats(info, format_str)

        if audio_only:
            ffmpeg_cmd = build_ffmpeg_single_cmd(
                fmt=resolved[0],
                global_headers=global_headers,
                audio_only=True,
                cookies_tempfile=cookies_tempfile,
            )
        elif len(resolved) == 2:
            video_fmt, audio_fmt = resolved[0], resolved[1]
            if video_fmt.get("vcodec", "none") in (None, "none", ""):
                video_fmt, audio_fmt = audio_fmt, video_fmt
            ffmpeg_cmd = build_ffmpeg_merge_cmd(
                video_fmt=video_fmt,
                audio_fmt=audio_fmt,
                global_headers=global_headers,
                cookies_tempfile=cookies_tempfile,
            )
        else:
            ffmpeg_cmd = build_ffmpeg_single_cmd(
                fmt=resolved[0],
                global_headers=global_headers,
                audio_only=False,
                cookies_tempfile=cookies_tempfile,
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

    finally:
        if cookies_tempfile and os.path.exists(cookies_tempfile):
            try:
                os.unlink(cookies_tempfile)
            except Exception:
                pass
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass


def _build_cookie_opts(cookiefile: Optional[str], cookiesfrombrowser: Optional[str]) -> dict:
    opts = {}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if cookiesfrombrowser:
        opts["cookiesfrombrowser"] = cookiesfrombrowser
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
    cookiefile: Optional[str] = Query(None, description="Path to cookies file"),
    cookiesfrombrowser: Optional[str] = Query(None, description="Browser to extract cookies from (e.g., chrome, firefox)"),
):
    """Extract video metadata without downloading."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **_build_cookie_opts(cookiefile, cookiesfrombrowser),
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
    cookiefile: Optional[str] = Query(None, description="Path to cookies file"),
    cookiesfrombrowser: Optional[str] = Query(None, description="Browser to extract cookies from (e.g., chrome, firefox)"),
):
    """
    Stream / download video.

    - `quality`: simple preset — 1080, 720, 480, 360 (default: 1080)
    - `format`: raw yt-dlp format string (overrides quality)
    - Automatically handles DASH/HLS manifests and video-only+audio merges.
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
            ydl_opts=_build_cookie_opts(cookiefile, cookiesfrombrowser),
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
    cookiefile: Optional[str] = Query(None, description="Path to cookies file"),
    cookiesfrombrowser: Optional[str] = Query(None, description="Browser to extract cookies from (e.g., chrome, firefox)"),
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
            ydl_opts=_build_cookie_opts(cookiefile, cookiesfrombrowser),
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
    cookiefile: Optional[str] = Query(None, description="Path to cookies file"),
    cookiesfrombrowser: Optional[str] = Query(None, description="Browser to extract cookies from (e.g., chrome, firefox)"),
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
            ydl_opts=_build_cookie_opts(cookiefile, cookiesfrombrowser),
        )
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
