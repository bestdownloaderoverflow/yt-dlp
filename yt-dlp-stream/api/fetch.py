"""Fetch endpoint for metadata and download links."""

import asyncio

from typing import Optional

import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request

from core.error_mapping import map_generic_exception, map_yt_dlp_exception
from core.redis_cache import download_cache
from core.helpers import _enforce_rate_limit
from ytdl_manager import ydl_manager

router = APIRouter()

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
DIRECT_PROXY_PLATFORMS = {"twitter", "x"}


def detect_content_type(info: dict) -> str:
    """Detect if content is video, photos, or playlist."""
    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if entries and entries[0].get("formats"):
            first_fmt = entries[0]["formats"][0]
            if first_fmt.get("video_ext") == "jpg":
                return "photos"
        return "playlist"

    formats = info.get("formats", [])
    if formats:
        first_fmt = formats[0]
        if first_fmt.get("video_ext") == "jpg":
            return "photos"
    return "video"


def get_available_qualities(formats: list) -> list:
    """Get available quality options from formats."""
    qualities = []
    heights = set()

    for f in formats:
        height = f.get("height")
        if height and height > 0:
            heights.add(height)

    # Return common qualities that are available
    for q in [1080, 720, 480, 360]:
        if any(h >= q for h in heights):
            qualities.append(f"{q}p")

    return qualities if qualities else ["best"]


async def generate_video_links(url: str, info: dict, qualities: list, platform: str) -> dict:
    """Generate download links for video qualities with session integrity."""
    links = {}
    formats = info.get("formats", [])
    global_headers = info.get("http_headers") or {}
    title = info.get("title")
    use_direct_proxy = platform in DIRECT_PROXY_PLATFORMS

    for quality in qualities:
        # Find format for this quality
        height = int(quality.replace("p", "")) if quality.endswith("p") else None
        fmt = None
        if height is not None:
            for f in formats:
                if f.get("height") == height and f.get("protocol") in ("http", "https", ""):
                    fmt = f
                    break

        # Fallback to best format if no exact match
        if not fmt:
            for f in formats:
                if f.get("protocol") in ("http", "https", ""):
                    fmt = f
                    break

        # Prepare session data — use pre-computed headers from the format dict
        # (built by ydl_manager._build_outbound_headers_locked during extract_info)
        # to avoid acquiring an extra ydl_manager slot just for header lookup.
        direct_url = None
        http_headers = None
        cookies = None

        if fmt and use_direct_proxy:
            direct_url = fmt.get("url")
            fmt_headers = fmt.get("http_headers") or global_headers
            http_headers = {
                k: v for k, v in fmt_headers.items()
                if k.lower() not in ("cookie", "accept-encoding")
            }
            cookies = fmt_headers.get("Cookie") or fmt_headers.get("cookie")

        key = await download_cache.create_session(
            url=url,
            type="video",
            quality=quality.replace("p", ""),
            direct_url=direct_url,
            http_headers=http_headers,
            cookies=cookies,
            platform=platform,
            title=title,
            proxy=info.get("__fetch_proxy"),
            impersonate=info.get("__fetch_impersonate"),
        )
        links[quality] = f"/download?key={key}"
    return links


async def generate_mp3_link(url: str, platform: str, title: Optional[str], proxy: Optional[str], impersonate: Optional[str]) -> str:
    """Generate download link for MP3."""
    key = await download_cache.create_session(
        url=url,
        type="mp3",
        platform=platform,
        title=title,
        proxy=proxy,
        impersonate=impersonate,
    )
    return f"/download?key={key}"


def get_photo_url(formats: list) -> str:
    """Get original quality photo URL from formats."""
    # Try to get 'orig' quality first
    for f in formats:
        if f.get("format_id") == "orig":
            return f.get("url")
    # Fallback to last format (usually highest quality)
    if formats:
        return formats[-1].get("url")
    return None


async def generate_photo_links(url: str, info: dict, proxy: Optional[str] = None, impersonate: Optional[str] = None) -> list:
    """Generate download links for photos."""
    photos = []

    if info.get("_type") == "playlist":
        # Multi photos
        entries = info.get("entries", [])
        for idx, entry in enumerate(entries, 1):
            formats = entry.get("formats", [])
            photo_url = get_photo_url(formats)
            key = await download_cache.create_session(
                url=url, type="photo", photo_index=idx,
                proxy=proxy, impersonate=impersonate,
            )
            photos.append({
                "index": idx,
                "width": entry.get("width"),
                "height": entry.get("height"),
                "url": photo_url,
                "download_link": f"/download?key={key}",
            })
    else:
        # Single photo
        formats = info.get("formats", [])
        photo_url = get_photo_url(formats)
        key = await download_cache.create_session(
            url=url, type="photo", photo_index=1,
            proxy=proxy, impersonate=impersonate,
        )
        photos.append({
            "index": 1,
            "width": info.get("width"),
            "height": info.get("height"),
            "url": photo_url,
            "download_link": f"/download?key={key}",
        })

    return photos


@router.get("/fetch")
async def fetch(
    url: str = Query(..., description="Video/photo URL"),
    proxy: Optional[str] = Query(None, description="Proxy URL"),
    impersonate: Optional[str] = Query(
        None, description="Browser to impersonate for TLS fingerprinting"
    ),
    request: Request = None,
):
    """
    Fetch metadata and generate encrypted download links.

    Download links expire in 5 minutes (300 seconds).

    Returns:
        - type: video | photos | playlist
        - platform: youtube | twitter | tiktok | etc
        - download_links: Object with video qualities and mp3 link
        - photos: Array of photo objects with download links
        - expires_in: TTL in seconds
    """
    _enforce_rate_limit(request)

    try:
        # Gunakan ydl_manager untuk session integrity (CookieJar persistent)
        info = await asyncio.wait_for(
            asyncio.to_thread(
                ydl_manager.extract_info,
                url,
                proxy,
                impersonate,
            ),
            timeout=30,
        )

        if not info:
            raise HTTPException(status_code=400, detail="Could not extract video info")

        content_type = detect_content_type(info)
        platform = info.get("extractor_key", "unknown").lower()
        # Persist request-level settings in info for session creation.
        info["__fetch_proxy"] = proxy
        info["__fetch_impersonate"] = impersonate

        response = {
            "type": content_type,
            "platform": platform,
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "expires_in": 300,
        }

        if content_type == "video":
            qualities = get_available_qualities(info.get("formats", []))
            response["download_links"] = {
                "video": await generate_video_links(url, info, qualities, platform),
                "mp3": await generate_mp3_link(
                    url=url,
                    platform=platform,
                    title=info.get("title"),
                    proxy=proxy,
                    impersonate=impersonate,
                ),
            }

        elif content_type == "photos":
            response["photos"] = await generate_photo_links(url, info, proxy=proxy, impersonate=impersonate)

        elif content_type == "playlist":
            response["playlist_count"] = info.get("playlist_count")
            response["entries"] = [
                {
                    "index": i,
                    "title": e.get("title"),
                    "duration": e.get("duration"),
                }
                for i, e in enumerate(info.get("entries", []), 1)
            ]

        return response

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Extraction timed out")
    except yt_dlp.utils.DownloadError as e:
        raise map_yt_dlp_exception(e, default_status=400)
    except Exception as e:
        raise map_generic_exception(e, default_status=500)
