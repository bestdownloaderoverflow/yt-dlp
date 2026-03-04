"""Download endpoint using encrypted keys."""

import asyncio
import logging
from typing import AsyncIterator, Optional

import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse, RedirectResponse
from urllib.parse import quote

from core.redis_cache import download_cache
from core.generators import (
    _chunked_video_generator_with_disconnect,
    _stream_chunked_merge,
    _stream_mp3_chunked,
)
from core.helpers import (
    _extract_info_and_resolve,
    can_use_chunked_streaming,
    can_use_chunked_streaming_multi,
    _build_ydl_opts,
)
from api.stream_ffmpeg import _streaming_response

router = APIRouter()
logger = logging.getLogger("ytdlp_stream")

QUALITY_FORMATS = {
    "1080": "best[height<=1080][ext=mp4]/best[height<=1080]",
    "720": "best[height<=720][ext=mp4]/best[height<=720]",
    "480": "best[height<=480][ext=mp4]/best[height<=480]",
    "360": "best[height<=360][ext=mp4]/best[height<=360]",
}

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
VIDEO_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB


@router.get("/download")
async def download(
    key: str = Query(..., description="Download key from /fetch"),
    download: bool = Query(True, description="Force download as attachment"),
    request: FastAPIRequest = None,
):
    """
    Download video, mp3, or photo using encrypted key.

    Key expires in 5 minutes after being generated.
    For Twitter/X videos, uses direct URL proxy with session integrity headers.
    """
    session = download_cache.get_session(key)
    if not session:
        raise HTTPException(
            status_code=404, detail="Download link expired or invalid"
        )

    url = session.url
    type = session.type

    try:
        if type == "video":
            # Use direct URL proxy if available (for Twitter/X session integrity)
            if session.direct_url and session.http_headers:
                return await _download_video_direct(
                    session.direct_url,
                    session.http_headers,
                    session.cookies,
                    download,
                    request,
                )
            # Fallback to traditional method
            return await _download_video(url, session.quality, download, request)
        elif type == "mp3":
            return await _download_mp3(url, download, request)
        elif type == "photo":
            return await _download_photo(url, session.photo_index, download, request)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown type: {type}")

    except Exception as e:
        logger.exception(f"Error in download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _download_video_direct(
    direct_url: str,
    http_headers: dict,
    cookies: Optional[str],
    download: bool,
    request: FastAPIRequest,
):
    """Download video using direct URL proxy with session integrity headers (for Twitter/X)."""
    # Build safe headers (remove accept-encoding to avoid compression issues)
    safe_headers = {
        k: v for k, v in http_headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    safe_headers["Accept-Encoding"] = "identity"

    # Add cookies if present
    if cookies:
        safe_headers["Cookie"] = cookies

    # Extract filename from URL or use default
    filename = "video.mp4"

    async def _stream_video() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=300
        ) as client:
            async with client.stream(
                "GET", direct_url, headers=safe_headers
            ) as resp:
                resp.raise_for_status()
                async for data in resp.aiter_bytes(65536):
                    if await request.is_disconnected():
                        logger.info("Client disconnected from direct video stream")
                        break
                    yield data

    response_headers = _build_disposition_header(filename, download)

    return StreamingResponse(
        _stream_video(),
        media_type="video/mp4",
        headers=response_headers,
    )


async def _download_video(
    url: str, quality: str, download: bool, request: FastAPIRequest
):
    """Download video using chunked method."""
    format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["1080"])

    info, resolved, global_headers = await asyncio.to_thread(
        _extract_info_and_resolve, url, format_str, None, None
    )

    base_filename = info.get("title", "download") or "download"
    out_filename = f"{base_filename}.mp4"

    can_chunk = len(resolved) == 1 and can_use_chunked_streaming(resolved[0])
    can_chunk_multi = len(resolved) == 2 and can_use_chunked_streaming_multi(resolved)

    if can_chunk:
        fmt = resolved[0]
        direct_url = fmt["url"]
        http_headers = fmt.get("http_headers") or global_headers
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

        if filesize:
            ydl_refresh_info = {
                "url": url,
                "format_str": format_str,
                "ydl_opts": {},
            }
            body = _chunked_video_generator_with_disconnect(
                direct_url,
                http_headers,
                filesize,
                VIDEO_CHUNK_SIZE,
                ydl_refresh_info,
                request,
            )
        else:
            safe_headers = {
                k: v for k, v in http_headers.items() if k.lower() != "accept-encoding"
            }

            async def _simple_stream() -> AsyncIterator[bytes]:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=60
                ) as client:
                    async with client.stream(
                        "GET", direct_url, headers=safe_headers
                    ) as resp:
                        resp.raise_for_status()
                        async for data in resp.aiter_bytes(65536):
                            if await request.is_disconnected():
                                logger.info("Client disconnected")
                                break
                            yield data

            body = _simple_stream()

        response_headers = _build_disposition_header(out_filename, download)
        if filesize:
            response_headers["Content-Length"] = str(filesize)

        return StreamingResponse(
            body,
            media_type="video/mp4",
            headers=response_headers,
        )

    elif can_chunk_multi:
        video_fmt, audio_fmt = resolved[0], resolved[1]
        if video_fmt.get("vcodec", "none") in (None, "none", ""):
            video_fmt, audio_fmt = audio_fmt, video_fmt

        body = _stream_chunked_merge(
            video_fmt,
            audio_fmt,
            global_headers,
            {"proxy": None, "impersonate": None},
            url,
            format_str,
            request,
        )

        response_headers = _build_disposition_header(out_filename, download)
        return StreamingResponse(
            body,
            media_type="video/mp4",
            headers=response_headers,
        )

    else:
        return await _streaming_response(
            url=url,
            format_str=format_str,
            audio_only=False,
            download=download,
            ydl_opts={},
            request=request,
        )


async def _download_mp3(url: str, download: bool, request: FastAPIRequest):
    """Download MP3 using chunked method."""
    info, resolved, global_headers = await asyncio.to_thread(
        _extract_info_and_resolve, url, AUDIO_FORMAT, None, None
    )

    base_filename = info.get("title", "download") or "download"
    out_filename = f"{base_filename}.mp3"

    if len(resolved) == 1 and can_use_chunked_streaming(resolved[0]):
        fmt = resolved[0]
        body = _stream_mp3_chunked(fmt, global_headers, {}, url, request)

        response_headers = _build_disposition_header(out_filename, download)
        return StreamingResponse(
            body,
            media_type="audio/mpeg",
            headers=response_headers,
        )
    else:
        return await _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts={},
            request=request,
        )


async def _download_photo(
    url: str, photo_index: int, download: bool, request: FastAPIRequest
):
    """Download photo as attachment (proxy stream)."""
    # Use ydl_manager for session integrity (preserves cookies)
    from ytdl_manager import ydl_manager

    info = ydl_manager.extract_info(url)

    photo_url = None
    width = None
    height = None

    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if 1 <= photo_index <= len(entries):
            entry = entries[photo_index - 1]
            formats = entry.get("formats", [])
            width = entry.get("width")
            height = entry.get("height")
            for f in formats:
                if f.get("format_id") == "orig":
                    photo_url = f.get("url")
                    break
            if not photo_url and formats:
                photo_url = formats[-1].get("url")
    else:
        formats = info.get("formats", [])
        width = info.get("width")
        height = info.get("height")
        for f in formats:
            if f.get("format_id") == "orig":
                photo_url = f.get("url")
                break
        if not photo_url and formats:
            photo_url = formats[-1].get("url")

    if not photo_url:
        raise HTTPException(status_code=404, detail="Photo not found")

    # Stream photo as attachment instead of redirect
    ext = photo_url.split("?")[0].split(".")[-1] if "." in photo_url else "jpg"
    filename = f"photo_{photo_index}.{ext}"

    async def _stream_photo() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            async with client.stream("GET", photo_url) as resp:
                resp.raise_for_status()
                async for data in resp.aiter_bytes(65536):
                    if await request.is_disconnected():
                        logger.info("Client disconnected from photo stream")
                        break
                    yield data

    response_headers = _build_disposition_header(filename, download)

    return StreamingResponse(
        _stream_photo(),
        media_type=f"image/{ext if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else 'jpeg'}",
        headers=response_headers,
    )


def _build_disposition_header(filename: str, download: bool) -> dict:
    """Build Content-Disposition header."""
    disposition = "attachment" if download else "inline"
    ascii_name = filename.encode("ascii", "ignore").decode()
    utf8_name = quote(filename, safe="")

    if ascii_name == filename:
        cd_header = f'{disposition}; filename="{filename.replace(chr(34), chr(92)+chr(34))}"'
    else:
        cd_header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

    return {
        "Content-Disposition": cd_header,
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }
