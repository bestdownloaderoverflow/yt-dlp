"""FFmpeg-based streaming endpoints."""

import logging
from typing import Optional

import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from core.config import QUALITY_FORMATS, AUDIO_FORMAT
from core.generators import stream_generator_ffmpeg
from core.helpers import _enforce_rate_limit, _build_ydl_opts

router = APIRouter()
logger = logging.getLogger("ytdl_stream")


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


@router.get("/stream/video")
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
        error_msg = str(e)
        if "rate-limited" in error_msg.lower() or "try again later" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "RATE_LIMITED",
                    "message": error_msg,
                    "retry_after": 300,
                }
            )
        raise HTTPException(status_code=400, detail=error_msg)
    except Exception as e:
        error_msg = str(e)
        if "rate" in error_msg.lower() and "limit" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "RATE_LIMITED",
                    "message": error_msg,
                    "retry_after": 300,
                }
            )
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/stream/mp3")
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
        error_msg = str(e)
        if "rate-limited" in error_msg.lower() or "try again later" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "RATE_LIMITED",
                    "message": error_msg,
                    "retry_after": 300,
                }
            )
        raise HTTPException(status_code=400, detail=error_msg)
    except Exception as e:
        error_msg = str(e)
        if "rate" in error_msg.lower() and "limit" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "RATE_LIMITED",
                    "message": error_msg,
                    "retry_after": 300,
                }
            )
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/stream/audio")
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
        error_msg = str(e)
        if "rate-limited" in error_msg.lower() or "try again later" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "RATE_LIMITED",
                    "message": error_msg,
                    "retry_after": 300,
                }
            )
        raise HTTPException(status_code=400, detail=error_msg)
    except Exception as e:
        error_msg = str(e)
        if "rate" in error_msg.lower() and "limit" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "RATE_LIMITED",
                    "message": error_msg,
                    "retry_after": 300,
                }
            )
        raise HTTPException(status_code=500, detail=error_msg)
