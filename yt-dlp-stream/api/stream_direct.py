"""Direct streaming endpoint (no ffmpeg)."""

import asyncio
import logging
from typing import Optional, AsyncIterator

import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from core.config import AUDIO_FORMAT_M4A
from core.generators import stream_generator_direct
from core.helpers import _enforce_rate_limit, _build_ydl_opts, _build_internal_chunked_stream

router = APIRouter()
logger = logging.getLogger("ytdl_stream")


@router.get("/stream/m4a")
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
