"""Chunked streaming endpoints (Cobalt-style)."""

import asyncio
import logging
from typing import Optional, AsyncIterator

import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from core.config import QUALITY_FORMATS, AUDIO_FORMAT, VIDEO_CHUNK_SIZE, estimate_video_size, estimate_mp3_size
from core.delivery import plan_delivery
from core.error_mapping import map_generic_exception, map_yt_dlp_exception
from core.generators import (
    _chunked_video_generator_with_disconnect,
    _stream_chunked_merge,
    _stream_mp3_chunked,
)
from core.helpers import _enforce_rate_limit, _extract_info_and_resolve, _build_ydl_opts, needs_ios_video_transcode
from api.stream_ffmpeg import _streaming_response

router = APIRouter()
logger = logging.getLogger("ytdl_stream")

_VIDEO_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
}


@router.get("/stream/video-chunked")
async def stream_video_chunked(
    url: str = Query(..., description="Video URL"),
    quality: Optional[str] = Query(None, description="Quality preset: 1080, 720, 480, 360"),
    format: Optional[str] = Query(None, description="Custom yt-dlp format string (overrides quality)"),
    download: bool = Query(False, description="Force download as attachment"),
    strict: bool = Query(False, description="Enable stricter yt-dlp-like delivery behavior"),
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
    - `strict`: prefer stricter yt-dlp-like delivery behavior
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
                    ydl_opts=ydl_opts,
                    request=request,
                )

        if strict and delivery.mode == "multi_progressive":
            logger.info("Strict mode: using ffmpeg merge path for multi-track progressive format")
            return await _streaming_response(
                url=url,
                format_str=format_str,
                audio_only=False,
                download=download,
                ydl_opts=ydl_opts,
                request=request,
            )

        if strict and delivery.mode == "single_progressive":
            # yt-dlp HttpFD default behavior is single request stream (no forced chunked ranges)
            fmt = resolved[0]
            out_ext = (fmt.get("ext") or "mp4").lower()
            out_filename = f"{base_filename}.{out_ext}"
            media_type = _VIDEO_MIME_BY_EXT.get(out_ext, "application/octet-stream")
            direct_url = fmt["url"]
            http_headers = fmt.get("http_headers") or global_headers
            safe_headers = {k: v for k, v in http_headers.items() if k.lower() != "accept-encoding"}
            safe_headers["Accept-Encoding"] = "identity"

            async def _strict_simple_stream() -> AsyncIterator[bytes]:
                async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                    async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                        resp.raise_for_status()
                        async for data in resp.aiter_bytes(65536):
                            if await request.is_disconnected():
                                logger.info("Client disconnected, stopping strict video stream")
                                break
                            yield data

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
            filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            if filesize:
                response_headers["Content-Length"] = str(filesize)

            return StreamingResponse(
                _strict_simple_stream(),
                media_type=media_type,
                headers=response_headers,
            )

        if delivery.mode == "single_progressive":
                # Single progressive download file
                fmt = resolved[0]
                out_ext = (fmt.get("ext") or "mp4").lower()
                out_filename = f"{base_filename}.{out_ext}"
                media_type = _VIDEO_MIME_BY_EXT.get(out_ext, "application/octet-stream")
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
                    safe_headers["Accept-Encoding"] = "identity"
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
                    media_type=media_type,
                    headers=response_headers,
                )

        elif delivery.mode == "multi_progressive":
            out_filename = f"{base_filename}.mp4"
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
            logger.info("Format requires ffmpeg streaming: %s", delivery.reason)
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
        raise map_yt_dlp_exception(e, default_status=400)
    except Exception as e:
        logger.exception("Error in stream_video_chunked")
        raise map_generic_exception(e, default_status=500)


@router.get("/stream/mp3-chunked")
async def stream_mp3_chunked(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Force download as attachment"),
    strict: bool = Query(False, description="Enable stricter yt-dlp-like delivery behavior"),
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
    - `strict`: prefer stricter yt-dlp-like delivery behavior
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
        delivery = plan_delivery(resolved)

        if strict:
            logger.info("Strict mode: using ffmpeg audio path")
            return await _streaming_response(
                url=url,
                format_str=AUDIO_FORMAT,
                audio_only=True,
                download=download,
                ydl_opts=ydl_opts,
                request=request,
            )

        if delivery.mode == "single_progressive":
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
            logger.info("Format requires ffmpeg streaming: %s", delivery.reason)
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
        raise map_yt_dlp_exception(e, default_status=400)
    except Exception as e:
        logger.exception("Error in stream_mp3_chunked")
        raise map_generic_exception(e, default_status=500)
