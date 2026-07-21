"""REST routes compatible with tiktok-api-dl / Douyin gateway."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from gateway import extraction_cache
from gateway import session as session_store
from gateway.engine_adapter import EngineError, get_engine
from gateway.normalize import (
    CDN_UA,
    build_content_disposition,
    build_response,
    sanitize_filename_part,
)
from gateway.slideshow import (
    SlideshowError,
    cleanup_temp,
    open_file_stream,
    render_slideshow,
)

logger = logging.getLogger("gateway.routes")

BUFFER_SIZE = 256 * 1024
EXTRACTION_TIMEOUT_SECONDS = int(os.environ.get("EXTRACTION_TIMEOUT_SECONDS", "120"))
MAX_CONCURRENT_EXTRACTIONS = int(os.environ.get("MAX_CONCURRENT_EXTRACTIONS", "8"))
MAX_CONCURRENT_SLIDESHOW = int(os.environ.get("SLIDESHOW_MAX_CONCURRENT", "1"))
TIKTOK_API_KEY = os.environ.get("TIKTOK_API_KEY", "")
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept, X-API-Key",
}

_extraction_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
_slideshow_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SLIDESHOW)


class ExtractRequest(BaseModel):
    url: Optional[str] = None
    version: Optional[str] = Field(default="v1")
    proxy: Optional[str] = None
    cookie: Optional[str] = None
    impersonate: Optional[str] = None


def _json_response(body: Any, status: int = 200, extra: Optional[dict] = None) -> Response:
    headers = {"Content-Type": "application/json; charset=utf-8", **CORS_HEADERS}
    if extra:
        headers.update(extra)
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        media_type="application/json",
        status_code=status,
        headers=headers,
    )


def _error_json(message: str, status: int = 500, code: Optional[str] = None) -> Response:
    body: Dict[str, str] = {"error": message}
    if code:
        body["code"] = code
    return _json_response(body, status)


def _check_api_key(request: Request) -> bool:
    if not TIKTOK_API_KEY:
        return True
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
    return hmac.compare_digest(key, TIKTOK_API_KEY)


def _is_supported_url(url: str) -> bool:
    return bool(
        re.search(
            r"tiktok\.com|douyin\.com|iesdouyin|vt\.tiktok|vm\.tiktok|amemv\.com",
            url,
            re.I,
        )
    )


async def extract_post(
    url: str,
    options: Dict[str, Any],
    *,
    force_platform: Optional[str] = None,
) -> Dict[str, Any]:
    engine = get_engine()

    async def _extract():
        platform, item = await engine.extract_detail(
            url,
            platform=force_platform,
            proxy=options.get("proxy"),
            cookie=options.get("cookie"),
        )
        # Cache serializable item only
        return {"platform": platform, "item": item}

    async with _extraction_semaphore:
        cached = await asyncio.wait_for(
            extraction_cache.get_or_extract(url, options, _extract),
            timeout=EXTRACTION_TIMEOUT_SECONDS,
        )

    platform = force_platform or cached.get("platform") or "tiktok"
    item = cached.get("item") or cached
    return await build_response(item, platform, options)


async def _stream_direct(
    session: Dict[str, Any],
    filename: str,
    content_type: str,
    download: bool,
    request: Request,
    session_key: str,
) -> Response:
    direct_url = session.get("direct_url") or ""
    if not direct_url:
        await session_store.restore_session(session)
        return _error_json("No media URL available in session", 400, "bad_request")

    base_headers = dict(session.get("http_headers") or {})
    headers = {
        "User-Agent": CDN_UA,
        **base_headers,
    }
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), follow_redirects=True)
    try:
        req = client.build_request("GET", direct_url, headers=headers)
        resp = await client.send(req, stream=True)

        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            await client.aclose()
            await session_store.restore_session(session)
            return _error_json(
                f"Upstream CDN returned {resp.status_code}",
                502 if resp.status_code >= 500 else resp.status_code,
                "upstream_error",
            )

        resp_headers = {
            "Content-Type": content_type,
            "Content-Disposition": build_content_disposition(filename, download),
            "X-Accel-Buffering": "no",
            **CORS_HEADERS,
        }
        cl = resp.headers.get("content-length")
        if cl:
            resp_headers["Content-Length"] = cl
        cr = resp.headers.get("content-range")
        if cr:
            resp_headers["Content-Range"] = cr

        async def content_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            content_generator(),
            media_type=content_type,
            headers=resp_headers,
            status_code=resp.status_code,
        )
    except Exception as exc:
        try:
            await client.aclose()
        except Exception:
            pass
        await session_store.restore_session(session)
        return _error_json(f"Stream error: {exc}", 502, "upstream_error")


async def _stream_slideshow(
    session: Dict[str, Any],
    author: str,
    download: bool,
) -> Response:
    photo_urls = session.get("photo_urls") or []
    audio_url = session.get("audio_url")
    referer = session.get("referer") or "https://www.tiktok.com/"

    if not photo_urls:
        await session_store.restore_session(session)
        return _error_json("No photos available for slideshow", 400, "bad_request")

    async with _slideshow_semaphore:
        try:
            result = await render_slideshow(
                photo_urls,
                audio_url,
                referer=referer,
                ffmpeg_path=FFMPEG_PATH,
            )
        except SlideshowError as exc:
            await session_store.restore_session(session)
            return _error_json(str(exc), exc.status, "slideshow_error")
        except Exception as exc:
            await session_store.restore_session(session)
            return _error_json(f"Slideshow failed: {exc}", 502, "slideshow_error")

    output_path = result["output_path"]
    temp_dir = result["temp_dir"]
    file_size = result["file_size"]
    fh = open_file_stream(output_path)

    async def stream_mp4():
        try:
            loop = asyncio.get_running_loop()
            while True:
                chunk = await loop.run_in_executor(None, fh.read, BUFFER_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            fh.close()
            cleanup_temp(temp_dir)

    filename = f"{author}_slideshow.mp4"
    resp_headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": build_content_disposition(filename, download),
        "Content-Length": str(file_size),
        "X-Accel-Buffering": "no",
        **CORS_HEADERS,
    }
    return StreamingResponse(stream_mp4(), media_type="video/mp4", headers=resp_headers)


def _handle_extract_errors(exc: Exception) -> Response:
    if isinstance(exc, asyncio.TimeoutError):
        return _error_json("Extraction timed out", 504, "timeout")
    if isinstance(exc, EngineError):
        return _error_json(str(exc), exc.status, exc.code)
    msg = str(exc)
    if re.search(r"blocked|private|restricted|403|captcha|verify", msg, re.I):
        return _error_json(msg, 403, "ip_blocked")
    if re.search(r"not found|Unable to|Could not extract|Cannot", msg, re.I):
        return _error_json(msg, 400, "not_found")
    logger.exception("unhandled extract error")
    return _error_json(msg, 500, "internal_error")


def register_routes(app: FastAPI) -> None:
    @app.post("/tiktok")
    async def handle_tiktok(req: ExtractRequest, request: Request) -> Response:
        if not _check_api_key(request):
            return _error_json("Invalid or missing API Key", 401, "unauthorized")

        url = (req.url or "").strip()
        if not url:
            return _error_json("URL is required", 400, "bad_request")
        if not _is_supported_url(url):
            return _error_json(
                "Only TikTok/Douyin URLs are supported", 400, "bad_request"
            )

        options = {
            "url": url,
            "version": req.version or "v1",
            "proxy": req.proxy.strip() if req.proxy else None,
            "cookie": req.cookie.strip() if req.cookie else None,
            "impersonate": req.impersonate.strip() if req.impersonate else None,
        }
        # Hybrid: auto-detect platform from URL
        try:
            result = await extract_post(url, options, force_platform=None)
            return _json_response(result)
        except Exception as exc:
            return _handle_extract_errors(exc)

    @app.post("/douyin")
    async def handle_douyin(req: ExtractRequest, request: Request) -> Response:
        if not _check_api_key(request):
            return _error_json("Invalid or missing API Key", 401, "unauthorized")

        url = (req.url or "").strip()
        if not url:
            return _error_json("URL is required", 400, "bad_request")
        if not _is_supported_url(url):
            return _error_json(
                "Only TikTok/Douyin URLs are supported", 400, "bad_request"
            )

        options = {
            "url": url,
            "version": req.version or "v1",
            "proxy": req.proxy.strip() if req.proxy else None,
            "cookie": req.cookie.strip() if req.cookie else None,
            "platform": "douyin",
        }
        try:
            # Prefer douyin if URL ambiguous
            force = "douyin" if "douyin" in url or "iesdouyin" in url else None
            result = await extract_post(url, options, force_platform=force)
            return _json_response(result)
        except Exception as exc:
            return _handle_extract_errors(exc)

    @app.get("/tiktok/download")
    async def handle_download(request: Request) -> Response:
        key = (request.query_params.get("key") or "").strip()
        if not key:
            return _error_json("Missing key query parameter", 400, "bad_request")

        raw_dl = (request.query_params.get("download") or "true").strip().lower()
        download = raw_dl not in ("0", "false", "no", "off")

        session = await session_store.claim_session(key)
        if not session:
            return _error_json("Download link expired or invalid", 404, "not_found")

        author = sanitize_filename_part(session.get("author"), "tiktok")
        session_type = session.get("type", "video")

        try:
            if session_type == "slideshow":
                return await _stream_slideshow(session, author, download)
            if session_type == "video":
                quality = session.get("quality") or "video"
                return await _stream_direct(
                    session,
                    f"{author}_{quality}.mp4",
                    "video/mp4",
                    download,
                    request,
                    key,
                )
            if session_type == "photo":
                idx = session.get("photo_index") or 1
                return await _stream_direct(
                    session,
                    f"{author}_photo_{idx}.jpg",
                    "image/jpeg",
                    download,
                    request,
                    key,
                )
            if session_type == "mp3":
                return await _stream_direct(
                    session, f"{author}.mp3", "audio/mpeg", download, request, key
                )
            await session_store.restore_session(session)
            return _error_json(f"Unknown content type: {session_type}", 400, "bad_request")
        except SlideshowError as exc:
            await session_store.restore_session(session)
            return _error_json(str(exc), exc.status, "slideshow_error")
        except Exception as exc:
            logger.exception("unhandled error in /tiktok/download")
            await session_store.restore_session(session)
            return _error_json(f"Failed to stream media: {exc}", 502, "upstream_error")
