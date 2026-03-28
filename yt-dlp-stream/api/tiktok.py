"""TikTok downloader endpoint with video, photo, and slideshow support."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from core.error_mapping import (
    is_dns_failure_message,
    is_geo_restricted_message,
    map_generic_exception,
    map_yt_dlp_exception,
)
from core.generators import slideshow_stream_generator
from core.helpers import _enforce_rate_limit
from core.redis_cache import download_cache
from core.config import estimate_video_size, estimate_mp3_size
from ytdl_manager import ydl_manager

router = APIRouter()
logger = logging.getLogger("ytdl_stream")


class TikTokRequest(BaseModel):
    """Request body for TikTok URL submission."""
    url: str
    proxy: Optional[str] = None
    impersonate: Optional[str] = None  # Set to "chrome" or "safari" if impersonate dependencies are installed


class TikTokAuthor(BaseModel):
    """Author information for TikTok content."""
    nickname: str
    uniqueId: str
    signature: str = ""
    avatar: str = ""
    avatarThumb: str = ""
    avatarMedium: str = ""
    avatarLarger: str = ""


class TikTokStatistics(BaseModel):
    """Statistics for TikTok content."""
    play_count: int = 0
    digg_count: int = 0
    comment_count: int = 0
    share_count: int = 0


class TikTokVideoResponse(BaseModel):
    """Response for TikTok video."""
    status: str = "tunnel"
    title: str = ""
    description: str = ""
    statistics: TikTokStatistics
    artist: str = ""
    cover: str = ""
    duration: int = 0  # milliseconds
    audio: Optional[str] = None
    download_link: dict
    music_duration: int = 0
    author: TikTokAuthor


class TikTokPhotoResponse(BaseModel):
    """Response for TikTok photo slideshow."""
    status: str = "picker"
    title: str = ""
    description: str = ""
    statistics: TikTokStatistics
    artist: str = ""
    cover: str = ""
    duration: int = 0
    audio: Optional[str] = None
    download_link: dict
    photos: list
    download_slideshow: Optional[str] = None
    author: TikTokAuthor


def is_tiktok_url(url: str) -> bool:
    """Check if URL is a TikTok or Douyin URL."""
    return "tiktok.com" in url or "douyin.com" in url


def normalize_tiktok_url(url: str) -> str:
    """Normalize obvious user-input mistakes before sending to yt-dlp."""
    normalized = (url or "").strip()
    if normalized.startswith("httpsr://"):
        normalized = "https://" + normalized[len("httpsr://"):]
    elif normalized.startswith("httpr://"):
        normalized = "http://" + normalized[len("httpr://"):]
    elif "://" not in normalized and is_tiktok_url(normalized):
        normalized = f"https://{normalized.lstrip('/')}"
    return normalized


def validate_tiktok_url(url: str) -> str:
    """Return a normalized TikTok URL or raise 400 for invalid input."""
    normalized = normalize_tiktok_url(url)
    parsed = urlparse(normalized)

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="TikTok URL must use http or https")

    if not parsed.netloc or not is_tiktok_url(normalized):
        raise HTTPException(status_code=400, detail="Only TikTok and Douyin URLs are supported")

    return normalized


def map_tiktok_exception(exc: BaseException, *, default_status: int = 500) -> HTTPException:
    message = str(exc)

    if is_dns_failure_message(message):
        return HTTPException(
            status_code=503,
            detail={"error": "UPSTREAM_DNS_FAILURE", "message": message},
        )

    if is_geo_restricted_message(message):
        return HTTPException(
            status_code=503,
            detail={"error": "GEO_RESTRICTED", "message": message},
        )

    if "comfortable for some audiences" in message.lower():
        return HTTPException(
            status_code=403,
            detail="TikTok requires an authenticated session for this content.",
        )

    mapped = map_generic_exception(exc, default_status=default_status)
    if mapped.status_code == 500:
        return HTTPException(status_code=500, detail=f"Internal server error: {message}")
    return mapped


def is_photo_content(info: dict) -> bool:
    """Check if content is a photo/slideshow post."""
    formats = info.get("formats", [])
    return any(
        f.get("format_id", "").startswith("image-")
        for f in formats
    )


def extract_author_info(info: dict) -> TikTokAuthor:
    """Extract author information from video info."""
    thumbnails = info.get("thumbnails", [])
    avatar_url = thumbnails[0].get("url", "") if thumbnails else ""

    return TikTokAuthor(
        nickname=info.get("uploader") or info.get("channel") or "unknown",
        uniqueId=info.get("uploader_id") or info.get("uploader") or "unknown",
        signature=info.get("description", ""),
        avatar=avatar_url,
        avatarThumb=avatar_url,
        avatarMedium=avatar_url,
        avatarLarger=avatar_url,
    )


def extract_statistics(info: dict) -> TikTokStatistics:
    """Extract statistics from video info."""
    return TikTokStatistics(
        play_count=info.get("view_count", 0),
        digg_count=info.get("like_count", 0),
        comment_count=info.get("comment_count", 0),
        share_count=info.get("repost_count", 0),
    )


def extract_format_headers(
    fmt: dict,
    info: dict,
    url: str = None,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
) -> tuple[dict, str]:
    """
    Extract headers and cookies from format using ydl_manager.
    Returns (headers, cookies) tuple.

    This mimics the serverpy approach of extracting cookies directly
    from the cookiejar using the format URL.
    """
    # Get global headers from info
    global_headers = info.get("http_headers", {})

    # Get format-specific headers
    fmt_headers = fmt.get("http_headers", {})

    # Merge headers (format headers override global)
    headers = {**global_headers, **fmt_headers}

    # Extract cookies using cookiejar directly (like serverpy)
    cookies = None
    fmt_url = fmt.get("url") or url

    if fmt_url:
        try:
            # Try to get cookies from ydl_manager's cookiejar
            cookiejar = ydl_manager.get_cookiejar(
                proxy=proxy,
                impersonate=impersonate,
            )
            if cookiejar and hasattr(cookiejar, 'get_cookie_header'):
                cookie_header = cookiejar.get_cookie_header(fmt_url)
                if cookie_header:
                    cookies = cookie_header
                    logger.debug(f"Extracted cookies for {fmt_url[:50]}...")
        except Exception as e:
            logger.debug(f"Could not extract cookies from cookiejar: {e}")

    # Fallback to calc_headers if no cookies from cookiejar
    if not cookies:
        try:
            calc_headers = ydl_manager.calc_headers(
                fmt,
                load_cookies=True,
                proxy=proxy,
                impersonate=impersonate,
            )
            cookies = calc_headers.get("Cookie") or calc_headers.get("cookie")
            # Add other headers from calc_headers
            for k, v in calc_headers.items():
                if k.lower() not in ("cookie", "accept-encoding"):
                    headers[k] = v
        except Exception as e:
            logger.debug(f"Could not calc_headers: {e}")

    # Clean up headers for httpx
    safe_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }

    return safe_headers, cookies


def _parse_content_length(headers: httpx.Headers) -> Optional[int]:
    """Parse file size from Content-Length or Content-Range headers."""
    content_length = headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)

    content_range = headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    return None


async def _probe_content_length(url: str, headers: dict) -> Optional[int]:
    """
    Best-effort probe to get exact content length from origin server.
    Falls back from HEAD to a ranged GET when needed.
    """
    if not url:
        return None

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            head_resp = await client.request("HEAD", url, headers=headers)
            size = _parse_content_length(head_resp.headers)
            if size and size > 0:
                return size

            range_headers = {**headers, "Range": "bytes=0-0"}
            range_resp = await client.get(url, headers=range_headers)
            size = _parse_content_length(range_resp.headers)
            if size and size > 0:
                return size
    except Exception as e:
        logger.debug(f"Could not probe content length: {e}")

    return None


def generate_video_download_links(
    info: dict,
    author: TikTokAuthor,
    request_url: str,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
) -> dict:
    """Generate encrypted download links for video formats."""
    formats = info.get("formats", [])
    links = {}
    duration_seconds = int(info.get("duration", 0) or 0)

    # Find video formats (with both video and audio codecs)
    video_formats = [
        f for f in formats
        if f.get("vcodec") and f["vcodec"] != "none"
        and f.get("acodec") and f["acodec"] != "none"
    ]

    # Find audio-only format
    audio_format = next(
        (f for f in formats
         if f.get("acodec") and f["acodec"] != "none"
         and (not f.get("vcodec") or f["vcodec"] == "none")),
        None
    )

    if not audio_format and video_formats:
        # Fallback to first video format for audio
        audio_format = video_formats[0]

    # Sort video formats by quality
    video_formats.sort(key=lambda x: (x.get("height", 0) * x.get("width", 0)), reverse=True)

    # Find different quality versions
    download_format = next((f for f in formats if f.get("format_id") == "download"), None)
    hd_formats = [f for f in video_formats if f.get("height", 0) >= 720]
    sd_formats = [f for f in video_formats if f.get("height", 0) < 720]

    # Extract headers/cookies once and reuse across all formats
    # (avoids repeated lock acquisition + cookiejar lookups)
    representative_fmt = download_format or (video_formats[0] if video_formats else audio_format)
    if representative_fmt is None:
        return links
    shared_headers, shared_cookies = extract_format_headers(
        representative_fmt, info, request_url, proxy=proxy, impersonate=impersonate
    )

    def _create_link(fmt, link_type, quality=None):
        key = download_cache.create_session(
            url=request_url,
            type=link_type,
            quality=quality,
            direct_url=fmt.get("url"),
            http_headers=shared_headers,
            cookies=shared_cookies,
            author=author.nickname,
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
            duration=duration_seconds,
        )
        return f"/tiktok/download?key={key}"

    # Generate download links with session keys
    if download_format:
        links["watermark"] = _create_link(download_format, "video", "watermark")

    if sd_formats:
        links["no_watermark"] = _create_link(sd_formats[0], "video", "no_watermark")

    if hd_formats:
        links["no_watermark_hd"] = _create_link(hd_formats[0], "video", "no_watermark_hd")

    if audio_format:
        links["mp3"] = _create_link(audio_format, "mp3")

    return links


def generate_photo_download_links(
    info: dict,
    author: TikTokAuthor,
    request_url: str,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
) -> tuple[dict, list, Optional[str]]:
    """Generate encrypted download links for photo formats."""
    formats = info.get("formats", [])

    # Get image formats
    image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
    audio_format = next((f for f in formats if f.get("format_id") == "audio"), None)
    duration_seconds = int(info.get("duration", 0) or 0)

    # Generate individual photo download links
    photo_keys = []
    photos = []
    for i, img in enumerate(image_formats):
        http_headers, cookies = extract_format_headers(
            img, info, request_url, proxy=proxy, impersonate=impersonate
        )

        key = download_cache.create_session(
            url=img["url"],
            type="photo",
            photo_index=i + 1,
            direct_url=img.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author.nickname,
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=img.get("filesize") or img.get("filesize_approx"),
            duration=duration_seconds,
        )
        download_url = f"/tiktok/download?key={key}"
        photo_keys.append(download_url)
        photos.append({"type": "photo", "url": download_url})

    links = {"no_watermark": photo_keys}

    # Add MP3 link if audio available
    if audio_format:
        http_headers, cookies = extract_format_headers(
            audio_format, info, request_url, proxy=proxy, impersonate=impersonate
        )

        key = download_cache.create_session(
            url=audio_format.get("url"),
            type="mp3",
            direct_url=audio_format.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author.nickname,
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=audio_format.get("filesize") or audio_format.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["mp3"] = f"/tiktok/download?key={key}"

    # Generate slideshow link
    photo_urls = [img["url"] for img in image_formats]
    audio_url = audio_format.get("url") if audio_format else None

    slideshow_key = download_cache.create_session(
        url=request_url,
        type="slideshow",
        photo_urls=photo_urls,
        audio_url=audio_url,
        author=author.nickname,
        platform="tiktok",
        proxy=proxy,
        impersonate=impersonate,
        duration=duration_seconds or (len(photo_urls) * 4),
    )
    slideshow_link = f"/tiktok/download?key={slideshow_key}"

    return links, photos, slideshow_link


@router.post("/tiktok")
async def process_tiktok(
    request: TikTokRequest,
    fastapi_request: FastAPIRequest = None,
):
    """
    Process TikTok URL and return metadata with encrypted download links.

    Supports:
    - Regular TikTok videos (with and without watermark)
    - Photo/slideshow posts (multiple images)
    - MP3 audio extraction
    """
    _enforce_rate_limit(fastapi_request)

    url = request.url

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    url = validate_tiktok_url(url)

    try:
        # Extract video info using ydl_manager for session integrity
        # Use impersonate for TLS fingerprinting if available (helps with TikTok)
        impersonate = request.impersonate  # May be None if not provided
        # yt-dlp extraction is blocking I/O; offload it to thread pool so
        # concurrent TikTok fetch requests do not stall the event loop.
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(
                    ydl_manager.extract_info,
                    url,
                    request.proxy,
                    impersonate,
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="TikTok extraction timed out. Please try again.",
            )

        if not info:
            raise HTTPException(
                status_code=400,
                detail="Could not extract video info"
            )

        # Extract common metadata
        author = extract_author_info(info)
        statistics = extract_statistics(info)
        duration_ms = int(info.get("duration", 0) * 1000) if info.get("duration") else 0

        # Check if photo or video content
        if is_photo_content(info):
            # Photo/slideshow post
            links, photos, slideshow_link = generate_photo_download_links(
                info, author, url, request.proxy, impersonate
            )

            # Find audio URL for response
            formats = info.get("formats", [])
            audio_format = next(
                (f for f in formats if f.get("format_id") == "audio"), None
            )

            return TikTokPhotoResponse(
                status="picker",
                title=info.get("title") or info.get("fulltitle", ""),
                description=info.get("description") or info.get("title", ""),
                statistics=statistics,
                artist=author.nickname,
                cover=info.get("thumbnail", ""),
                duration=duration_ms,
                audio=None,
                download_link=links,
                photos=photos,
                download_slideshow=slideshow_link,
                author=author,
            )

        else:
            # Video post
            links = generate_video_download_links(info, author, url, request.proxy, impersonate)

            # Find audio URL for response
            formats = info.get("formats", [])
            audio_format = next(
                (f for f in formats
                 if f.get("acodec") and f["acodec"] != "none"
                 and (not f.get("vcodec") or f["vcodec"] == "none")),
                None
            )

            return TikTokVideoResponse(
                status="tunnel",
                title=info.get("title") or info.get("fulltitle", ""),
                description=info.get("description") or info.get("title", ""),
                statistics=statistics,
                artist=author.nickname,
                cover=info.get("thumbnail", ""),
                duration=duration_ms,
                audio=None,
                download_link=links,
                music_duration=duration_ms,
                author=author,
            )

    except yt_dlp.utils.DownloadError as e:
        if is_dns_failure_message(str(e)) or is_geo_restricted_message(str(e)):
            raise map_tiktok_exception(e, default_status=503)
        raise map_yt_dlp_exception(
            e,
            default_status=400,
            enable_not_found_heuristics=True,
            forbidden_detail="Access denied. The video may be private or region-blocked.",
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Error processing TikTok URL: {e}")
        raise map_tiktok_exception(e, default_status=500)


def _build_disposition_header(filename: str, download: bool) -> dict:
    """Build Content-Disposition header."""
    disposition = "attachment" if download else "inline"
    ascii_name = filename.encode("ascii", "ignore").decode()
    utf8_name = quote(filename, safe="")

    if ascii_name == filename:
        cd_header = f'{disposition}; filename="{filename.replace(chr(34), chr(92)+chr(34))}"'
    else:
        cd_header = f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'

    return {
        "Content-Disposition": cd_header,
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }


async def _should_refresh_tiktok_stream(resp: httpx.Response) -> bool:
    """
    Refresh only when a 403 looks transient instead of permanent.
    """
    if resp.status_code != 403:
        return False

    try:
        text = (await resp.aread()).decode("utf-8", errors="ignore").lower()
    except Exception:
        return True

    permanent_patterns = (
        "geo_restricted",
        "geo restricted",
        "region",
        "country",
        "blocked",
        "permission",
        "do not have permission",
        "login",
        "log in",
        "captcha",
        "verify you are human",
        "access denied",
    )
    return not any(pattern in text for pattern in permanent_patterns)


async def _refresh_tiktok_url(original_url: str, proxy: Optional[str], impersonate: Optional[str]) -> Optional[dict]:
    """
    Refresh expired TikTok URL by re-extracting info.
    Returns new format dict with refreshed URL and headers.
    """
    try:
        original_url = validate_tiktok_url(original_url)
        logger.info(f"Refreshing TikTok URL: {original_url[:50]}...")
        # Keep refresh extraction off the event loop to prevent head-of-line
        # blocking when many streams refresh simultaneously.
        info = await asyncio.to_thread(
            ydl_manager.extract_info,
            original_url,
            proxy,
            impersonate,
        )
        return info
    except Exception as e:
        logger.error(f"Failed to refresh TikTok URL: {e}")
        return None


@router.get("/tiktok/download")
async def download_tiktok(
    key: str = Query(..., description="Download key from /tiktok"),
    download: bool = Query(True, description="Force download as attachment"),
    request: FastAPIRequest = None,
):
    """
    Download TikTok video, photo, MP3, or generated slideshow.

    Types:
    - video: Stream video with session integrity headers
    - photo: Stream single photo
    - mp3: Stream audio
    - slideshow: Generate and stream slideshow video from photos
    """
    session = download_cache.get_session(key)
    if not session:
        raise HTTPException(
            status_code=404,
            detail="Download link expired or invalid"
        )

    content_type = session.type

    try:
        if content_type == "video":
            return await _download_video(session, download, request)
        elif content_type == "photo":
            return await _download_photo(session, download, request)
        elif content_type == "mp3":
            return await _download_mp3(session, download, request)
        elif content_type == "slideshow":
            return await _download_slideshow(session, download, request)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown content type: {content_type}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error in TikTok download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _tiktok_stream_with_refresh(
    direct_url: str,
    safe_headers: dict,
    request: FastAPIRequest,
    original_url: Optional[str],
    proxy: Optional[str],
    impersonate: Optional[str],
    format_finder,
    content_label: str = "content",
    timeout: int = 300,
    max_refreshes: int = 2,
):
    """Shared streaming helper with 403 URL refresh logic.

    Args:
        format_finder: callable(refreshed_info) -> format dict or None.
            Receives the re-extracted info dict and returns the new format to use,
            or raises HTTPException on irrecoverable error.
    """
    refresh_count = 0

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout
    ) as client:
        while True:
            try:
                async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                    if (
                        resp.status_code == 403
                        and refresh_count < max_refreshes
                        and original_url
                        and await _should_refresh_tiktok_stream(resp)
                    ):
                        logger.warning(
                            f"{content_label} URL expired (403), attempting refresh "
                            f"{refresh_count + 1}/{max_refreshes}"
                        )
                        refreshed_info = await _refresh_tiktok_url(
                            original_url, proxy, impersonate,
                        )
                        if refreshed_info:
                            new_fmt = format_finder(refreshed_info)
                            if new_fmt:
                                direct_url = new_fmt.get("url")
                                new_headers, new_cookies = extract_format_headers(
                                    new_fmt, refreshed_info, original_url,
                                    proxy=proxy, impersonate=impersonate,
                                )
                                safe_headers = {
                                    k: v for k, v in new_headers.items()
                                    if k.lower() not in ("accept-encoding", "cookie")
                                }
                                safe_headers["Accept-Encoding"] = "identity"
                                if new_cookies:
                                    safe_headers["Cookie"] = new_cookies
                                refresh_count += 1
                                logger.info(f"{content_label} URL refreshed successfully")
                                continue

                        logger.error(f"Failed to refresh {content_label} URL")
                        raise HTTPException(
                            status_code=403,
                            detail=f"{content_label} URL expired and could not be refreshed",
                        )

                    resp.raise_for_status()
                    async for data in resp.aiter_bytes(65536):
                        if await request.is_disconnected():
                            logger.info(f"Client disconnected from TikTok {content_label} stream")
                            break
                        yield data
                    break

            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error during TikTok {content_label} download: {e}")
                raise HTTPException(
                    status_code=e.response.status_code,
                    detail=f"Download failed: {e}",
                )
            except httpx.ConnectError as e:
                logger.warning(f"TikTok {content_label} stream connect error: {e}")
                raise HTTPException(
                    status_code=503,
                    detail={"error": "UPSTREAM_CONNECT_FAILURE", "message": str(e)},
                )
            except httpx.TimeoutException as e:
                logger.warning(f"TikTok {content_label} stream timeout: {e}")
                raise HTTPException(
                    status_code=503,
                    detail={"error": "UPSTREAM_TIMEOUT", "message": str(e)},
                )


def _build_safe_headers(http_headers: dict, cookies: Optional[str]) -> dict:
    """Build safe headers dict from session headers and cookies."""
    safe_headers = {
        k: v for k, v in http_headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    safe_headers["Accept-Encoding"] = "identity"
    if cookies:
        safe_headers["Cookie"] = cookies
    return safe_headers


async def _download_video(
    session,
    download: bool,
    request: FastAPIRequest,
):
    """Download TikTok video with session integrity headers and URL refresh on 403."""
    direct_url = session.direct_url
    if not direct_url:
        raise HTTPException(status_code=400, detail="No video URL available")

    quality = session.quality or "video"
    safe_headers = _build_safe_headers(session.http_headers or {}, session.cookies)

    author = session.author or "tiktok"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}_{quality}.mp4"

    def _find_video_format(refreshed_info):
        formats = refreshed_info.get("formats", [])
        video_formats = [
            f for f in formats
            if f.get("vcodec") and f["vcodec"] != "none"
            and f.get("acodec") and f["acodec"] != "none"
        ]
        video_formats.sort(key=lambda x: (x.get("height", 0) * x.get("width", 0)), reverse=True)
        if quality == "watermark":
            return next((f for f in formats if f.get("format_id") == "download"), None)
        elif quality == "no_watermark_hd" and video_formats:
            return video_formats[0]
        elif video_formats:
            return next((f for f in video_formats if f.get("height", 0) < 720), video_formats[0])
        return None

    response_headers = _build_disposition_header(filename, download)
    filesize = getattr(session, "filesize", None)
    duration = getattr(session, "duration", None)
    if filesize and int(filesize) > 0:
        response_headers["Content-Length"] = str(int(filesize))
    else:
        estimated_size = estimate_video_size(None, duration)
        if estimated_size > 0:
            response_headers["Estimated-Content-Length"] = str(estimated_size)

    return StreamingResponse(
        _tiktok_stream_with_refresh(
            direct_url=direct_url,
            safe_headers=safe_headers,
            request=request,
            original_url=session.url,
            proxy=session.proxy,
            impersonate=session.impersonate,
            format_finder=_find_video_format,
            content_label="Video",
            timeout=300,
        ),
        media_type="video/mp4",
        headers=response_headers,
    )


async def _download_photo(
    session,
    download: bool,
    request: FastAPIRequest,
):
    """Download TikTok photo with URL refresh on 403."""
    direct_url = session.direct_url
    if not direct_url:
        raise HTTPException(status_code=400, detail="No photo URL available")

    photo_index = session.photo_index or 1
    safe_headers = _build_safe_headers(session.http_headers or {}, session.cookies)

    author = session.author or "tiktok"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}_photo_{photo_index}.jpg"

    def _find_photo_format(refreshed_info):
        formats = refreshed_info.get("formats", [])
        image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
        if photo_index <= len(image_formats):
            return image_formats[photo_index - 1]
        logger.error(
            f"Photo index {photo_index} out of range after refresh "
            f"(got {len(image_formats)} images)"
        )
        raise HTTPException(status_code=404, detail="Photo no longer available after refresh")

    response_headers = _build_disposition_header(filename, download)
    filesize = getattr(session, "filesize", None)
    resolved_size = int(filesize) if filesize and int(filesize) > 0 else None
    if not resolved_size:
        resolved_size = await _probe_content_length(direct_url, safe_headers)
    if resolved_size and resolved_size > 0:
        response_headers["Content-Length"] = str(resolved_size)

    return StreamingResponse(
        _tiktok_stream_with_refresh(
            direct_url=direct_url,
            safe_headers=safe_headers,
            request=request,
            original_url=session.url,
            proxy=session.proxy,
            impersonate=session.impersonate,
            format_finder=_find_photo_format,
            content_label="Photo",
            timeout=60,
        ),
        media_type="image/jpeg",
        headers=response_headers,
    )


async def _download_mp3(
    session,
    download: bool,
    request: FastAPIRequest,
):
    """Download TikTok audio with URL refresh on 403."""
    direct_url = session.direct_url
    if not direct_url:
        raise HTTPException(status_code=400, detail="No audio URL available")

    safe_headers = _build_safe_headers(session.http_headers or {}, session.cookies)

    author = session.author or "tiktok"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}.mp3"

    def _find_audio_format(refreshed_info):
        formats = refreshed_info.get("formats", [])
        return next(
            (f for f in formats
             if f.get("acodec") and f["acodec"] != "none"
             and (not f.get("vcodec") or f["vcodec"] == "none")),
            None
        )

    response_headers = _build_disposition_header(filename, download)
    filesize = getattr(session, "filesize", None)
    duration = getattr(session, "duration", None)
    resolved_size = int(filesize) if filesize and int(filesize) > 0 else None
    if not resolved_size:
        resolved_size = await _probe_content_length(direct_url, safe_headers)
    if resolved_size and resolved_size > 0:
        response_headers["Content-Length"] = str(resolved_size)
    else:
        estimated_size = estimate_mp3_size(duration)
        if estimated_size > 0:
            response_headers["Estimated-Content-Length"] = str(estimated_size)

    return StreamingResponse(
        _tiktok_stream_with_refresh(
            direct_url=direct_url,
            safe_headers=safe_headers,
            request=request,
            original_url=session.url,
            proxy=session.proxy,
            impersonate=session.impersonate,
            format_finder=_find_audio_format,
            content_label="Audio",
            timeout=300,
        ),
        media_type="audio/mpeg",
        headers=response_headers,
    )


async def _download_slideshow(
    session,
    download: bool,
    request: FastAPIRequest,
):
    """Generate and download TikTok slideshow video."""
    photo_urls = session.photo_urls
    audio_url = session.audio_url
    original_url = session.url
    proxy = session.proxy
    impersonate = session.impersonate

    if not photo_urls:
        raise HTTPException(status_code=400, detail="No photos available for slideshow")

    # Only refresh if session is older than 5 minutes (URLs may have expired)
    session_age = None
    if hasattr(session, "created_at") and session.created_at:
        from datetime import datetime
        try:
            created = datetime.fromisoformat(session.created_at) if isinstance(session.created_at, str) else session.created_at
            session_age = (datetime.now() - created).total_seconds()
        except Exception:
            session_age = None

    if original_url and (session_age is None or session_age > 300):
        try:
            refreshed_info = await _refresh_tiktok_url(
                original_url,
                proxy,
                impersonate,
            )
            if refreshed_info:
                formats = refreshed_info.get("formats", [])
                image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
                audio_format = next((f for f in formats if f.get("format_id") == "audio"), None)

                if image_formats:
                    photo_urls = [img["url"] for img in image_formats]
                if audio_format:
                    audio_url = audio_format.get("url")
        except Exception as e:
            logger.warning(f"Could not refresh slideshow URLs: {e}")

    # Generate filename
    author = session.author or "tiktok"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}_slideshow.mp4"

    # Create temp directory
    temp_dir = tempfile.mkdtemp(prefix="tiktok_slideshow_")

    # Stream slideshow
    response_headers = _build_disposition_header(filename, download)
    duration = getattr(session, "duration", None) or (len(photo_urls) * 4)
    estimated_size = estimate_video_size(None, duration)
    if estimated_size > 0:
        response_headers["Estimated-Content-Length"] = str(estimated_size)

    async def _slideshow_with_cleanup():
        try:
            async for chunk in slideshow_stream_generator(
                image_urls=photo_urls,
                audio_url=audio_url,
                temp_dir=temp_dir,
                filename=filename,
            ):
                yield chunk
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    return StreamingResponse(
        _slideshow_with_cleanup(),
        media_type="video/mp4",
        headers=response_headers,
    )
