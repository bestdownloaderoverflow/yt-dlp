"""Fetch endpoint for metadata and download links."""

from typing import Optional

import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request

from core.redis_cache import download_cache
from core.helpers import _build_ydl_opts, _enforce_rate_limit

router = APIRouter()

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"


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


def generate_video_links(url: str, qualities: list) -> dict:
    """Generate download links for video qualities."""
    links = {}
    for quality in qualities:
        key = download_cache.create_session(
            url=url, type="video", quality=quality.replace("p", "")
        )
        links[quality] = f"/download?key={key}"
    return links


def generate_mp3_link(url: str) -> str:
    """Generate download link for MP3."""
    key = download_cache.create_session(url=url, type="mp3")
    return f"/download?key={key}"


def generate_photo_links(url: str, info: dict) -> list:
    """Generate download links for photos."""
    photos = []

    if info.get("_type") == "playlist":
        # Multi photos
        entries = info.get("entries", [])
        for idx, entry in enumerate(entries, 1):
            key = download_cache.create_session(
                url=url, type="photo", photo_index=idx
            )
            photos.append({
                "index": idx,
                "width": entry.get("width"),
                "height": entry.get("height"),
                "download_link": f"/download?key={key}",
            })
    else:
        # Single photo
        key = download_cache.create_session(url=url, type="photo", photo_index=1)
        photos.append({
            "index": 1,
            "width": info.get("width"),
            "height": info.get("height"),
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
        ydl_opts = _build_ydl_opts(proxy, impersonate)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        content_type = detect_content_type(info)
        platform = info.get("extractor_key", "unknown").lower()

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
                "video": generate_video_links(url, qualities),
                "mp3": generate_mp3_link(url),
            }

        elif content_type == "photos":
            response["photos"] = generate_photo_links(url, info)

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

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
