"""TikTok downloader endpoint with video, photo, and slideshow support."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from core.error_mapping import map_generic_exception, map_yt_dlp_exception
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

    # Generate download links with session keys
    if download_format:
        http_headers, cookies = extract_format_headers(
            download_format, info, request_url, proxy=proxy, impersonate=impersonate
        )

        key = download_cache.create_session(
            url=request_url,
            type="video",
            quality="watermark",
            direct_url=download_format.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author.nickname,
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=download_format.get("filesize") or download_format.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["watermark"] = f"/tiktok/download?key={key}"

    if sd_formats:
        fmt = sd_formats[0]
        http_headers, cookies = extract_format_headers(
            fmt, info, request_url, proxy=proxy, impersonate=impersonate
        )

        key = download_cache.create_session(
            url=request_url,
            type="video",
            quality="no_watermark",
            direct_url=fmt.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author.nickname,
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["no_watermark"] = f"/tiktok/download?key={key}"

    if hd_formats:
        fmt = hd_formats[0]
        http_headers, cookies = extract_format_headers(
            fmt, info, request_url, proxy=proxy, impersonate=impersonate
        )

        key = download_cache.create_session(
            url=request_url,
            type="video",
            quality="no_watermark_hd",
            direct_url=fmt.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author.nickname,
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["no_watermark_hd"] = f"/tiktok/download?key={key}"

    if audio_format:
        http_headers, cookies = extract_format_headers(
            audio_format, info, request_url, proxy=proxy, impersonate=impersonate
        )

        key = download_cache.create_session(
            url=request_url,
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

    # Create photo picker list
    photos = [{"type": "photo", "url": img["url"]} for img in image_formats]

    # Generate individual photo download links
    photo_keys = []
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
        photo_keys.append(f"/tiktok/download?key={key}")

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

    if not is_tiktok_url(url):
        raise HTTPException(
            status_code=400,
            detail="Only TikTok and Douyin URLs are supported"
        )

    try:
        # Extract video info using ydl_manager for session integrity
        # Use impersonate for TLS fingerprinting if available (helps with TikTok)
        impersonate = request.impersonate  # May be None if not provided
        info = ydl_manager.extract_info(
            url,
            proxy=request.proxy,
            impersonate=impersonate,
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
                audio=audio_format.get("url") if audio_format else None,
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
                audio=audio_format.get("url") if audio_format else None,
                download_link=links,
                music_duration=duration_ms,
                author=author,
            )

    except yt_dlp.utils.DownloadError as e:
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
        mapped = map_generic_exception(e, default_status=500)
        if mapped.status_code == 500:
            raise HTTPException(
                status_code=500,
                detail=f"Internal server error: {str(e)}"
            )
        raise mapped


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


async def _refresh_tiktok_url(original_url: str, proxy: Optional[str], impersonate: Optional[str]) -> Optional[dict]:
    """
    Refresh expired TikTok URL by re-extracting info.
    Returns new format dict with refreshed URL and headers.
    """
    try:
        logger.info(f"Refreshing TikTok URL: {original_url[:50]}...")
        info = ydl_manager.extract_info(
            original_url,
            proxy=proxy,
            impersonate=impersonate,  # May be None
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


async def _download_video(
    session,
    download: bool,
    request: FastAPIRequest,
):
    """Download TikTok video with session integrity headers and URL refresh on 403."""
    direct_url = session.direct_url
    http_headers = session.http_headers or {}
    cookies = session.cookies
    original_url = session.url
    proxy = session.proxy
    impersonate = session.impersonate

    if not direct_url:
        raise HTTPException(status_code=400, detail="No video URL available")

    # Build safe headers
    safe_headers = {
        k: v for k, v in http_headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    safe_headers["Accept-Encoding"] = "identity"

    if cookies:
        safe_headers["Cookie"] = cookies

    # Generate filename
    author = session.author or "tiktok"
    quality = session.quality or "video"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}_{quality}.mp4"

    # Track refresh attempts
    max_refreshes = 3
    refresh_count = 0

    async def _stream_with_refresh():
        nonlocal direct_url, safe_headers, refresh_count

        while True:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=300
                ) as client:
                    async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                        # Handle 403 - try to refresh URL
                        if resp.status_code == 403 and refresh_count < max_refreshes:
                            logger.warning(f"TikTok URL expired (403), attempting refresh {refresh_count + 1}/{max_refreshes}")

                            # Refresh URL by re-extracting
                            refreshed_info = await _refresh_tiktok_url(
                                original_url,
                                proxy,
                                impersonate,
                            )
                            if refreshed_info:
                                # Find new format with same quality
                                formats = refreshed_info.get("formats", [])
                                video_formats = [
                                    f for f in formats
                                    if f.get("vcodec") and f["vcodec"] != "none"
                                    and f.get("acodec") and f["acodec"] != "none"
                                ]

                                # Sort by quality and find matching quality
                                video_formats.sort(key=lambda x: (x.get("height", 0) * x.get("width", 0)), reverse=True)

                                new_fmt = None
                                if quality == "watermark":
                                    new_fmt = next((f for f in formats if f.get("format_id") == "download"), None)
                                elif quality == "no_watermark_hd" and video_formats:
                                    new_fmt = video_formats[0]  # Best quality
                                elif video_formats:
                                    new_fmt = next((f for f in video_formats if f.get("height", 0) < 720), video_formats[0])

                                if new_fmt:
                                    direct_url = new_fmt.get("url")
                                    http_headers, cookies = extract_format_headers(
                                        new_fmt,
                                        refreshed_info,
                                        original_url,
                                        proxy=proxy,
                                        impersonate=impersonate,
                                    )
                                    safe_headers = {
                                        k: v for k, v in http_headers.items()
                                        if k.lower() not in ("accept-encoding", "cookie")
                                    }
                                    safe_headers["Accept-Encoding"] = "identity"
                                    if cookies:
                                        safe_headers["Cookie"] = cookies

                                    refresh_count += 1
                                    logger.info(f"URL refreshed successfully, retrying with new URL")
                                    continue  # Retry with new URL

                            logger.error("Failed to refresh URL")
                            raise HTTPException(status_code=403, detail="Video URL expired and could not be refreshed")

                        resp.raise_for_status()
                        async for data in resp.aiter_bytes(65536):
                            if await request.is_disconnected():
                                logger.info("Client disconnected from TikTok video stream")
                                break
                            yield data
                        break  # Success, exit loop

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403 and refresh_count < max_refreshes:
                    logger.warning(f"HTTP 403 during stream, attempting refresh...")
                    refreshed_info = await _refresh_tiktok_url(
                        original_url,
                        proxy,
                        impersonate,
                    )
                    if refreshed_info:
                        formats = refreshed_info.get("formats", [])
                        video_formats = [
                            f for f in formats
                            if f.get("vcodec") and f["vcodec"] != "none"
                            and f.get("acodec") and f["acodec"] != "none"
                        ]
                        video_formats.sort(key=lambda x: (x.get("height", 0) * x.get("width", 0)), reverse=True)

                        new_fmt = None
                        if quality == "watermark":
                            new_fmt = next((f for f in formats if f.get("format_id") == "download"), None)
                        elif quality == "no_watermark_hd" and video_formats:
                            new_fmt = video_formats[0]
                        elif video_formats:
                            new_fmt = next((f for f in video_formats if f.get("height", 0) < 720), video_formats[0])

                        if new_fmt:
                            direct_url = new_fmt.get("url")
                            http_headers, cookies = extract_format_headers(
                                new_fmt,
                                refreshed_info,
                                original_url,
                                proxy=proxy,
                                impersonate=impersonate,
                            )
                            safe_headers = {
                                k: v for k, v in http_headers.items()
                                if k.lower() not in ("accept-encoding", "cookie")
                            }
                            safe_headers["Accept-Encoding"] = "identity"
                            if cookies:
                                safe_headers["Cookie"] = cookies

                            refresh_count += 1
                            continue  # Retry

                logger.error(f"HTTP error during TikTok video download: {e}")
                raise HTTPException(status_code=e.response.status_code, detail=f"Download failed: {e}")

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
        _stream_with_refresh(),
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
    photo_index = session.photo_index or 1
    original_url = session.url
    proxy = session.proxy
    impersonate = session.impersonate

    if not direct_url:
        raise HTTPException(status_code=400, detail="No photo URL available")

    # Generate filename
    author = session.author or "tiktok"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}_photo_{photo_index}.jpg"

    max_refreshes = 2
    refresh_count = 0

    async def _stream_photo():
        nonlocal direct_url, refresh_count

        while True:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=60
                ) as client:
                    async with client.stream("GET", direct_url) as resp:
                        if resp.status_code == 403 and refresh_count < max_refreshes and original_url:
                            logger.warning(f"Photo URL expired, attempting refresh")
                            # Try to refresh by re-extracting
                            refreshed_info = await _refresh_tiktok_url(
                                original_url,
                                proxy,
                                impersonate,
                            )
                            if refreshed_info:
                                formats = refreshed_info.get("formats", [])
                                image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
                                if photo_index <= len(image_formats):
                                    direct_url = image_formats[photo_index - 1].get("url")
                                    refresh_count += 1
                                    continue

                        resp.raise_for_status()
                        async for data in resp.aiter_bytes(65536):
                            if await request.is_disconnected():
                                logger.info("Client disconnected from TikTok photo stream")
                                break
                            yield data
                        break

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403 and refresh_count < max_refreshes:
                    refresh_count += 1
                    continue
                raise

    response_headers = _build_disposition_header(filename, download)
    filesize = getattr(session, "filesize", None)
    if filesize and int(filesize) > 0:
        response_headers["Content-Length"] = str(int(filesize))

    return StreamingResponse(
        _stream_photo(),
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
    http_headers = session.http_headers or {}
    cookies = session.cookies
    original_url = session.url
    proxy = session.proxy
    impersonate = session.impersonate

    if not direct_url:
        raise HTTPException(status_code=400, detail="No audio URL available")

    # Build safe headers
    safe_headers = {
        k: v for k, v in http_headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    safe_headers["Accept-Encoding"] = "identity"

    if cookies:
        safe_headers["Cookie"] = cookies

    # Generate filename
    author = session.author or "tiktok"
    safe_author = "".join(c if c.isalnum() else "_" for c in author)
    filename = f"{safe_author}.mp3"

    max_refreshes = 3
    refresh_count = 0

    async def _stream_audio():
        nonlocal direct_url, safe_headers, refresh_count

        while True:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=300
                ) as client:
                    async with client.stream("GET", direct_url, headers=safe_headers) as resp:
                        if resp.status_code == 403 and refresh_count < max_refreshes and original_url:
                            logger.warning(f"Audio URL expired, attempting refresh {refresh_count + 1}/{max_refreshes}")
                            refreshed_info = await _refresh_tiktok_url(
                                original_url,
                                proxy,
                                impersonate,
                            )
                            if refreshed_info:
                                formats = refreshed_info.get("formats", [])
                                audio_format = next(
                                    (f for f in formats
                                     if f.get("acodec") and f["acodec"] != "none"
                                     and (not f.get("vcodec") or f["vcodec"] == "none")),
                                    None
                                )
                                if audio_format:
                                    direct_url = audio_format.get("url")
                                    http_headers, cookies = extract_format_headers(
                                        audio_format,
                                        refreshed_info,
                                        original_url,
                                        proxy=proxy,
                                        impersonate=impersonate,
                                    )
                                    safe_headers = {
                                        k: v for k, v in http_headers.items()
                                        if k.lower() not in ("accept-encoding", "cookie")
                                    }
                                    safe_headers["Accept-Encoding"] = "identity"
                                    if cookies:
                                        safe_headers["Cookie"] = cookies
                                    refresh_count += 1
                                    continue

                        resp.raise_for_status()
                        async for data in resp.aiter_bytes(65536):
                            if await request.is_disconnected():
                                logger.info("Client disconnected from TikTok audio stream")
                                break
                            yield data
                        break

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403 and refresh_count < max_refreshes:
                    refresh_count += 1
                    continue
                raise

    response_headers = _build_disposition_header(filename, download)
    filesize = getattr(session, "filesize", None)
    duration = getattr(session, "duration", None)
    if filesize and int(filesize) > 0:
        response_headers["Content-Length"] = str(int(filesize))
    else:
        estimated_size = estimate_mp3_size(duration)
        if estimated_size > 0:
            response_headers["Estimated-Content-Length"] = str(estimated_size)

    return StreamingResponse(
        _stream_audio(),
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

    # Try to refresh if needed
    if original_url:
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

    return StreamingResponse(
        slideshow_stream_generator(
            image_urls=photo_urls,
            audio_url=audio_url,
            temp_dir=temp_dir,
            filename=filename,
        ),
        media_type="video/mp4",
        headers=response_headers,
    )
