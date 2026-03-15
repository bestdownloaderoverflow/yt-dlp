"""Helper functions for streaming and format handling."""

import asyncio
import logging
from typing import Optional, AsyncIterator

from fastapi import HTTPException, Request as FastAPIRequest

from security import rate_limiter, create_stream_token, validate_stream_token
from internal_tunnel import internal_tunnel

logger = logging.getLogger("ytdl_stream")


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
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Use the left-most IP as originating client when behind gateway/proxy.
        client_host = forwarded_for.split(",")[0].strip() or "unknown"
    else:
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
    from ytdl_manager import ydl_manager

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


def resolve_formats(info: dict, format_str: str, ydl) -> list[dict]:
    """
    Use yt-dlp's format selector to resolve the format string against the
    extracted info dict.  Returns a list of format dicts:
      - 1 item  -> single muxed stream (or manifest URL)
      - 2 items -> video-only + audio-only that must be merged by ffmpeg

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
