"""Map TikTokDownloader extractor item -> tunnel/picker response."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from gateway import session as session_store

EXTRACT_SOURCE = "tiktokdownloader"

CDN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Chinese type labels from extractor (zh_CN) + English fallbacks
_PHOTO_TYPES = {"图集", "实况", "photo", "image", "images", "slideshow"}
_VIDEO_TYPES = {"视频", "video"}


def _num(value: Any) -> int:
    if isinstance(value, (int, float)) and value == value and value != -1:
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def sanitize_filename_part(value: Optional[str], fallback: str = "media") -> str:
    if not value:
        return fallback
    cleaned = re.sub(r"[^a-zA-Z0-9]", "_", value)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or fallback


def _duration_ms(item: dict) -> int:
    """Parse HH:MM:SS or int seconds/ms into milliseconds-ish int for clients."""
    raw = item.get("duration")
    if isinstance(raw, (int, float)) and raw > 0:
        # TikTok extractor stores formatted string; Douyin video.duration is ms
        return int(raw) if raw > 1000 else int(raw * 1000)
    if isinstance(raw, str) and ":" in raw:
        parts = raw.split(":")
        try:
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                sec = parts[0] * 60 + parts[1]
            else:
                sec = parts[0]
            return sec * 1000
        except ValueError:
            return 0
    return 0


def _is_photo(item: dict) -> bool:
    t = str(item.get("type") or "")
    if t in _PHOTO_TYPES:
        return True
    downloads = item.get("downloads")
    return isinstance(downloads, list) and len(downloads) > 0 and t not in _VIDEO_TYPES


def _as_url_list(downloads: Any) -> List[str]:
    if not downloads:
        return []
    if isinstance(downloads, str):
        return [downloads] if downloads.startswith("http") else []
    if isinstance(downloads, list):
        out = []
        for d in downloads:
            if isinstance(d, str) and d.startswith("http"):
                out.append(d)
            elif isinstance(d, dict):
                u = d.get("url") or d.get("download") or ""
                if isinstance(u, str) and u.startswith("http"):
                    out.append(u)
        return out
    return []


def _get_referer(platform: str) -> str:
    return {
        "douyin": "https://www.douyin.com/",
        "tiktok": "https://www.tiktok.com/",
    }.get(platform, "https://www.tiktok.com/")


def _download_headers(platform: str) -> Dict[str, str]:
    return {
        "User-Agent": CDN_UA,
        "Referer": _get_referer(platform),
        "Accept-Encoding": "identity",
    }


def _build_author(item: dict) -> Dict[str, str]:
    nickname = str(item.get("nickname") or item.get("mark") or "unknown")
    unique_id = str(
        item.get("unique_id") or item.get("uid") or item.get("sec_uid") or "unknown"
    )
    signature = str(item.get("signature") or "")
    avatar = str(item.get("avatar") or item.get("static_cover") or "")
    return {
        "nickname": nickname,
        "uniqueId": unique_id,
        "signature": signature,
        "avatar": avatar,
        "avatarThumb": avatar,
        "avatarMedium": avatar,
        "avatarLarger": avatar,
    }


def _build_statistics(item: dict) -> Dict[str, int]:
    return {
        "play_count": _num(item.get("play_count")),
        "digg_count": _num(item.get("digg_count")),
        "comment_count": _num(item.get("comment_count")),
        "share_count": _num(item.get("share_count")),
    }


def build_content_disposition(filename: str, download: bool) -> str:
    disposition = "attachment" if download else "inline"
    ascii_name = re.sub(r"[^\x20-\x7E]", "", filename)
    safe = ascii_name.replace('"', "'")
    if ascii_name == filename:
        return f'{disposition}; filename="{safe}"'
    return f"{disposition}; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


async def build_response(
    item: dict,
    platform: str,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    author_info = _build_author(item)
    statistics = _build_statistics(item)
    duration = _duration_ms(item)
    cover = str(item.get("static_cover") or item.get("dynamic_cover") or "")
    desc = str(item.get("desc") or item.get("id") or "").strip()
    music_url = item.get("music_url") or None
    if isinstance(music_url, str) and not music_url.startswith("http"):
        music_url = None
    safe_author = sanitize_filename_part(author_info["nickname"], platform)
    headers = _download_headers(platform)
    video_id = str(item.get("id") or "")
    source_url = options.get("url") or item.get("_source_url") or ""

    base_meta = {
        "extract_source": EXTRACT_SOURCE,
        "title": desc,
        "description": desc,
        "statistics": statistics,
        "artist": author_info["nickname"],
        "cover": cover,
        "duration": duration,
        "audio": music_url or "",
        "music_duration": duration,
        "author": author_info,
        "platform": platform,
        "id": video_id,
    }

    if _is_photo(item):
        return await _build_photo(
            item,
            platform,
            video_id,
            source_url,
            safe_author,
            headers,
            music_url,
            duration,
            base_meta,
            options,
        )
    return await _build_video(
        item,
        platform,
        video_id,
        source_url,
        safe_author,
        headers,
        music_url,
        duration,
        base_meta,
        options,
    )


async def _create_media_session(
    *,
    source_url: str,
    media_type: str,
    direct_url: str = "",
    photo_urls: Optional[List[str]] = None,
    audio_url: Optional[str] = None,
    quality: str = "",
    photo_index: int = 0,
    headers: Dict[str, str],
    author: str,
    platform: str,
    aweme_id: str,
    duration: int,
    proxy: Optional[str],
) -> str:
    payload: Dict[str, Any] = {
        "url": source_url,
        "type": media_type,
        "direct_url": direct_url,
        "http_headers": headers,
        "author": author,
        "platform": platform,
        "proxy": proxy,
        "aweme_id": aweme_id,
        "duration": duration,
        "referer": headers.get("Referer", _get_referer(platform)),
    }
    if quality:
        payload["quality"] = quality
    if photo_index:
        payload["photo_index"] = photo_index
    if photo_urls is not None:
        payload["photo_urls"] = photo_urls
    if audio_url:
        payload["audio_url"] = audio_url
    return await session_store.create_session(payload)


async def _build_video(
    item: dict,
    platform: str,
    video_id: str,
    source_url: str,
    safe_author: str,
    headers: Dict[str, str],
    music_url: Optional[str],
    duration: int,
    base_meta: dict,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    urls = _as_url_list(item.get("downloads"))
    if not urls and isinstance(item.get("downloads"), str):
        u = item["downloads"]
        if u.startswith("http"):
            urls = [u]

    links: Dict[str, Any] = {}
    proxy = options.get("proxy")

    if urls:
        # Best quality as no_watermark_hd + no_watermark
        primary = urls[0]
        key_hd = await _create_media_session(
            source_url=source_url,
            media_type="video",
            direct_url=primary,
            quality="no_watermark_hd",
            headers=headers,
            author=safe_author,
            platform=platform,
            aweme_id=video_id,
            duration=duration,
            proxy=proxy,
        )
        links["no_watermark_hd"] = f"/tiktok/download?key={key_hd}"

        key_nwm = await _create_media_session(
            source_url=source_url,
            media_type="video",
            direct_url=primary,
            quality="no_watermark",
            headers=headers,
            author=safe_author,
            platform=platform,
            aweme_id=video_id,
            duration=duration,
            proxy=proxy,
        )
        links["no_watermark"] = f"/tiktok/download?key={key_nwm}"

        if len(urls) > 1:
            key_wm = await _create_media_session(
                source_url=source_url,
                media_type="video",
                direct_url=urls[-1],
                quality="watermark",
                headers=headers,
                author=safe_author,
                platform=platform,
                aweme_id=video_id,
                duration=duration,
                proxy=proxy,
            )
            links["watermark"] = f"/tiktok/download?key={key_wm}"

    if music_url:
        mp3_key = await _create_media_session(
            source_url=source_url,
            media_type="mp3",
            direct_url=music_url,
            headers=headers,
            author=safe_author,
            platform=platform,
            aweme_id=video_id,
            duration=duration,
            proxy=proxy,
        )
        links["mp3"] = f"/tiktok/download?key={mp3_key}"

    return {
        "status": "tunnel",
        **base_meta,
        "download_link": links,
    }


async def _build_photo(
    item: dict,
    platform: str,
    video_id: str,
    source_url: str,
    safe_author: str,
    headers: Dict[str, str],
    music_url: Optional[str],
    duration: int,
    base_meta: dict,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    image_urls = _as_url_list(item.get("downloads"))
    proxy = options.get("proxy")
    photo_keys: List[str] = []
    photos: List[Dict[str, Any]] = []

    for i, img_url in enumerate(image_urls):
        key = await _create_media_session(
            source_url=source_url,
            media_type="photo",
            direct_url=img_url,
            photo_index=i + 1,
            headers=headers,
            author=safe_author,
            platform=platform,
            aweme_id=video_id,
            duration=duration,
            proxy=proxy,
        )
        link = f"/tiktok/download?key={key}"
        photo_keys.append(link)
        photos.append({"type": "photo", "url": img_url, "download_link": link})

    links: Dict[str, Any] = {"no_watermark": photo_keys}

    if music_url:
        mp3_key = await _create_media_session(
            source_url=source_url,
            media_type="mp3",
            direct_url=music_url,
            headers=headers,
            author=safe_author,
            platform=platform,
            aweme_id=video_id,
            duration=duration,
            proxy=proxy,
        )
        links["mp3"] = f"/tiktok/download?key={mp3_key}"

    slideshow_duration = duration or (len(image_urls) * 4 * 1000)
    slideshow_key = await _create_media_session(
        source_url=source_url,
        media_type="slideshow",
        photo_urls=image_urls,
        audio_url=music_url,
        headers=headers,
        author=safe_author,
        platform=platform,
        aweme_id=video_id,
        duration=slideshow_duration,
        proxy=proxy,
    )

    return {
        "status": "picker",
        **base_meta,
        "download_link": links,
        "photos": photos,
        "download_slideshow": f"/tiktok/download?key={slideshow_key}",
    }
