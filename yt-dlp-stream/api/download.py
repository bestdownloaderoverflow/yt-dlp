"""Download endpoint using encrypted keys."""

import asyncio
import logging
import os
from typing import AsyncIterator, Optional

import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse, RedirectResponse
from urllib.parse import quote

from core.redis_cache import download_cache
from core.config import QUALITY_FORMATS
from core.delivery import plan_delivery
from core.error_mapping import map_generic_exception, map_yt_dlp_exception
from core.generators import (
    _chunked_video_generator_with_disconnect,
    _stream_chunked_merge,
    _stream_mp3_chunked,
)
from core.helpers import (
    _extract_info_and_resolve,
    _build_ydl_opts,
    needs_ios_video_transcode,
)
from api.stream_ffmpeg import _streaming_response

router = APIRouter()
logger = logging.getLogger("ytdlp_stream")
DIRECT_PROXY_PLATFORMS = {"twitter", "x"}

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
VIDEO_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB
VIDEO_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
}


@router.get("/download")
async def download(
    key: str = Query(..., description="Download key from /fetch"),
    download: bool = Query(True, description="Force download as attachment"),
    strict: bool = Query(False, description="Enable stricter yt-dlp-like delivery behavior"),
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
            if (
                session.platform in DIRECT_PROXY_PLATFORMS
                and session.direct_url
                and session.http_headers
            ):
                return await _download_video_direct(
                    session.direct_url,
                    session.http_headers,
                    session.cookies,
                    session.title,
                    download,
                    request,
                    url=url,
                    proxy=session.proxy,
                    impersonate=session.impersonate,
                )
            # Fallback to traditional method
            return await _download_video(
                url,
                session.quality,
                download,
                request,
                strict=strict,
                proxy=session.proxy,
                impersonate=session.impersonate,
            )
        elif type == "mp3":
            return await _download_mp3(
                url,
                download,
                request,
                strict=strict,
                proxy=session.proxy,
                impersonate=session.impersonate,
            )
        elif type == "photo":
            return await _download_photo(
                url, session.photo_index, download, request,
                proxy=session.proxy, impersonate=session.impersonate,
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown type: {type}")

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise map_yt_dlp_exception(e, default_status=400)
    except Exception as e:
        logger.exception(f"Error in download: {e}")
        raise map_generic_exception(e, default_status=500)


async def _download_video_direct(
    direct_url: str,
    http_headers: dict,
    cookies: Optional[str],
    title: Optional[str],
    download: bool,
    request: FastAPIRequest,
    url: Optional[str] = None,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
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

    # Build filename from title + direct URL extension
    default_ext = (os.path.splitext(direct_url.split("?")[0])[1].lstrip(".") or "mp4").lower()
    base_filename = (title or "download").strip() or "download"
    filename = f"{base_filename}.{default_ext}"
    media_type = VIDEO_MIME_BY_EXT.get(default_ext, "application/octet-stream")

    max_refreshes = 2
    refresh_count = 0

    async def _stream_video() -> AsyncIterator[bytes]:
        nonlocal direct_url, safe_headers, refresh_count

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=300
        ) as client:
            while True:
                try:
                    async with client.stream(
                        "GET", direct_url, headers=safe_headers
                    ) as resp:
                        if resp.status_code == 403 and refresh_count < max_refreshes and url:
                            logger.warning(
                                f"Direct video URL expired (403), refresh {refresh_count + 1}/{max_refreshes}"
                            )
                            from ytdl_manager import ydl_manager
                            try:
                                info = await asyncio.wait_for(
                                    asyncio.to_thread(ydl_manager.extract_info, url, proxy, impersonate),
                                    timeout=30,
                                )
                                if info:
                                    formats = info.get("formats", [])
                                    new_fmt = next(
                                        (f for f in formats
                                         if f.get("protocol") in ("http", "https", "")
                                         and f.get("url")),
                                        None,
                                    )
                                    if new_fmt:
                                        direct_url = new_fmt["url"]
                                        new_headers = new_fmt.get("http_headers") or info.get("http_headers") or {}
                                        safe_headers = {
                                            k: v for k, v in new_headers.items()
                                            if k.lower() not in ("accept-encoding", "cookie")
                                        }
                                        safe_headers["Accept-Encoding"] = "identity"
                                        try:
                                            calc = ydl_manager.calc_headers(new_fmt, load_cookies=True, proxy=proxy, impersonate=impersonate)
                                            c = calc.get("Cookie") or calc.get("cookie")
                                            if c:
                                                safe_headers["Cookie"] = c
                                        except Exception:
                                            pass
                                        refresh_count += 1
                                        continue
                            except Exception as exc:
                                logger.error(f"Refresh failed: {exc}")
                            raise HTTPException(status_code=403, detail="Video URL expired and could not be refreshed")

                        resp.raise_for_status()
                        async for data in resp.aiter_bytes(65536):
                            if await request.is_disconnected():
                                logger.info("Client disconnected from direct video stream")
                                break
                            yield data
                        break

                except httpx.HTTPStatusError as e:
                    raise HTTPException(status_code=e.response.status_code, detail=f"Download failed: {e}")

    response_headers = _build_disposition_header(filename, download)

    return StreamingResponse(
        _stream_video(),
        media_type=media_type,
        headers=response_headers,
    )


async def _download_video(
    url: str,
    quality: str,
    download: bool,
    request: FastAPIRequest,
    strict: bool = False,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
):
    """Download video using chunked method."""
    format_str = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["1080"])

    try:
        info, resolved, global_headers = await asyncio.wait_for(
            asyncio.to_thread(
                _extract_info_and_resolve, url, format_str, proxy, impersonate
            ),
            timeout=30,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Video extraction timed out")

    base_filename = info.get("title", "download") or "download"
    delivery = plan_delivery(resolved)

    if delivery.mode == "single_progressive":
        fmt = resolved[0]
        if needs_ios_video_transcode(fmt):
            logger.info(
                "Single progressive codec '%s' is not iOS-friendly, using ffmpeg transcode path",
                fmt.get("vcodec"),
            )
            return await _streaming_response(
                url=url,
                format_str=format_str,
                audio_only=False,
                download=download,
                ydl_opts=_build_ydl_opts(proxy, impersonate),
                request=request,
            )

        out_ext = (fmt.get("ext") or "mp4").lower()
        out_filename = f"{base_filename}.{out_ext}"
        media_type = VIDEO_MIME_BY_EXT.get(out_ext, "application/octet-stream")
        direct_url = fmt["url"]
        http_headers = fmt.get("http_headers") or global_headers
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

        if strict:
            safe_headers = {
                k: v for k, v in http_headers.items() if k.lower() != "accept-encoding"
            }
            safe_headers["Accept-Encoding"] = "identity"

            async def _strict_simple_stream() -> AsyncIterator[bytes]:
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

            body = _strict_simple_stream()
        elif filesize:
            ydl_opts = _build_ydl_opts(proxy, impersonate)
            ydl_refresh_info = {
                "url": url,
                "format_str": format_str,
                "ydl_opts": ydl_opts,
                "target": "single",
                "format_id": fmt.get("format_id"),
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
            media_type=media_type,
            headers=response_headers,
        )

    elif delivery.mode == "multi_progressive":
        if strict:
            logger.info("Strict mode: using ffmpeg merge path for multi-track progressive format")
            return await _streaming_response(
                url=url,
                format_str=format_str,
                audio_only=False,
                download=download,
                ydl_opts=_build_ydl_opts(proxy, impersonate),
                request=request,
            )

        out_filename = f"{base_filename}.mp4"
        video_fmt, audio_fmt = resolved[0], resolved[1]
        if video_fmt.get("vcodec", "none") in (None, "none", ""):
            video_fmt, audio_fmt = audio_fmt, video_fmt

        body = _stream_chunked_merge(
            video_fmt,
            audio_fmt,
            global_headers,
            _build_ydl_opts(proxy, impersonate),
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
        logger.info("Using ffmpeg live delivery for video download: %s", delivery.reason)
        return await _streaming_response(
            url=url,
            format_str=format_str,
            audio_only=False,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
            request=request,
        )


async def _download_mp3(
    url: str,
    download: bool,
    request: FastAPIRequest,
    strict: bool = False,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
):
    """Download MP3 using chunked method."""
    try:
        info, resolved, global_headers = await asyncio.wait_for(
            asyncio.to_thread(
                _extract_info_and_resolve, url, AUDIO_FORMAT, proxy, impersonate
            ),
            timeout=30,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Audio extraction timed out")

    base_filename = info.get("title", "download") or "download"
    out_filename = f"{base_filename}.mp3"
    delivery = plan_delivery(resolved)

    if strict:
        logger.info("Strict mode: using ffmpeg audio path")
        return await _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
            request=request,
        )

    if delivery.mode == "single_progressive":
        fmt = resolved[0]
        body = _stream_mp3_chunked(
            fmt,
            global_headers,
            _build_ydl_opts(proxy, impersonate),
            url,
            request,
        )

        response_headers = _build_disposition_header(out_filename, download)
        return StreamingResponse(
            body,
            media_type="audio/mpeg",
            headers=response_headers,
        )
    else:
        logger.info("Using ffmpeg live delivery for MP3 download: %s", delivery.reason)
        return await _streaming_response(
            url=url,
            format_str=AUDIO_FORMAT,
            audio_only=True,
            download=download,
            ydl_opts=_build_ydl_opts(proxy, impersonate),
            request=request,
        )


async def _download_photo(
    url: str,
    photo_index: int,
    download: bool,
    request: FastAPIRequest,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
):
    """Download photo as attachment (proxy stream)."""
    from ytdl_manager import ydl_manager

    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(ydl_manager.extract_info, url, proxy, impersonate),
            timeout=30,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Photo extraction timed out")

    photo_url = None
    photo_fmt = None

    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if 1 <= photo_index <= len(entries):
            entry = entries[photo_index - 1]
            formats = entry.get("formats", [])
            for f in formats:
                if f.get("format_id") == "orig":
                    photo_fmt = f
                    break
            if not photo_fmt and formats:
                photo_fmt = formats[-1]
    else:
        formats = info.get("formats", [])
        for f in formats:
            if f.get("format_id") == "orig":
                photo_fmt = f
                break
        if not photo_fmt and formats:
            photo_fmt = formats[-1]

    if photo_fmt:
        photo_url = photo_fmt.get("url")

    if not photo_url:
        raise HTTPException(status_code=404, detail="Photo not found")

    # Build headers with session integrity (cookies, user-agent, etc.)
    global_headers = info.get("http_headers") or {}
    fmt_headers = photo_fmt.get("http_headers") or global_headers
    safe_headers = {
        k: v for k, v in fmt_headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    safe_headers["Accept-Encoding"] = "identity"

    # Extract cookies via calc_headers for session integrity
    try:
        calc_headers = ydl_manager.calc_headers(
            photo_fmt, load_cookies=True, proxy=proxy, impersonate=impersonate
        )
        cookies = calc_headers.get("Cookie") or calc_headers.get("cookie")
        if cookies:
            safe_headers["Cookie"] = cookies
    except Exception:
        pass

    ext = photo_url.split("?")[0].split(".")[-1] if "." in photo_url else "jpg"
    filename = f"photo_{photo_index}.{ext}"

    async def _stream_photo() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            async with client.stream("GET", photo_url, headers=safe_headers) as resp:
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
