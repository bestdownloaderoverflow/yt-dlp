#!/usr/bin/env python3
"""
Lightweight extractor daemon for Go gateway IPC.

Protocol:
- Input: one JSON object per line from stdin
- Output: one JSON object per line to stdout

Request shape:
{
  "id": "req-123",
  "method": "extract_info",
  "params": {
    "url": "https://...",
    "proxy": "http://...",
    "impersonate": "chrome"
  }
}
"""

import json
import logging
import os
import signal
import sys
import traceback
from typing import Any, Dict, Optional

import yt_dlp

# Ensure local project imports work when started from arbitrary cwd.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ytdl_manager import ydl_manager  # noqa: E402

DIRECT_PROXY_PLATFORMS = {"twitter", "x"}


logger = logging.getLogger("extractor_daemon")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

_RUNNING = True
_DOWNLOAD_CACHE = None


def _handle_stop(_signum: int, _frame: Any) -> None:
    global _RUNNING
    _RUNNING = False


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT, _handle_stop)


def _write_message(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _get_download_cache():
    global _DOWNLOAD_CACHE
    if _DOWNLOAD_CACHE is None:
        from core.redis_cache import download_cache as _dc  # lazy import
        _DOWNLOAD_CACHE = _dc
    return _DOWNLOAD_CACHE


def _error_response(req_id: Any, message: str, code: str = "internal_error", status: int = 500) -> Dict[str, Any]:
    return {
        "id": req_id,
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "status": status,
        },
    }


def _serialize_formats(info: Dict[str, Any]) -> list:
    return [
        {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "height": f.get("height"),
            "width": f.get("width"),
            "filesize": f.get("filesize"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "tbr": f.get("tbr"),
            "protocol": f.get("protocol"),
        }
        for f in info.get("formats", [])
    ]


def _detect_content_type(info: Dict[str, Any]) -> str:
    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if entries and entries[0].get("formats"):
            first_formats = entries[0]["formats"]
            if any(fmt.get("format_id", "").startswith("image-") for fmt in first_formats):
                return "photos"
            first_fmt = first_formats[0]
            if first_fmt.get("video_ext") == "jpg":
                return "photos"
        return "playlist"
    formats = info.get("formats", [])
    if formats:
        if any(fmt.get("format_id", "").startswith("image-") for fmt in formats):
            return "photos"
        first_fmt = formats[0]
        if first_fmt.get("video_ext") == "jpg":
            return "photos"
    return "video"


def _extract_source(info: Dict[str, Any], platform: str) -> Optional[str]:
    """Return extraction source marker for observability."""
    if platform in {"tiktok", "douyin"}:
        src = info.get("__tiktok_extract_source")
        if isinstance(src, str) and src:
            return src
        return "web"
    return None


def _get_available_qualities(formats: list) -> list:
    heights = set()
    for fmt in formats:
        height = fmt.get("height")
        if height and height > 0:
            heights.add(height)
    qualities = []
    for q in [1080, 720, 480, 360]:
        if any(h >= q for h in heights):
            qualities.append(f"{q}p")
    return qualities if qualities else ["best"]


def _get_photo_url(formats: list) -> Optional[str]:
    for fmt in formats:
        if fmt.get("format_id") == "orig":
            return fmt.get("url")
    for fmt in formats:
        if fmt.get("format_id", "").startswith("image-") and fmt.get("url"):
            return fmt.get("url")
    if formats:
        for fmt in formats:
            if fmt.get("url"):
                return fmt.get("url")
        return formats[-1].get("url")
    return None


def _generate_mp3_link(url: str, platform: str, title: Optional[str], proxy: Optional[str], impersonate: Optional[str]) -> str:
    key = _get_download_cache().create_session(
        url=url,
        type="mp3",
        platform=platform,
        title=title,
        proxy=proxy,
        impersonate=impersonate,
    )
    return f"/download?key={key}"


def _generate_video_links(url: str, info: Dict[str, Any], qualities: list, platform: str) -> Dict[str, str]:
    links: Dict[str, str] = {}
    formats = info.get("formats", [])
    global_headers = info.get("http_headers") or {}
    title = info.get("title")
    use_direct_proxy = platform in DIRECT_PROXY_PLATFORMS
    proxy = info.get("__fetch_proxy")
    impersonate = info.get("__fetch_impersonate")

    for quality in qualities:
        height = int(quality.replace("p", "")) if quality.endswith("p") else None
        chosen = None
        if height is not None:
            for fmt in formats:
                if fmt.get("height") == height and fmt.get("protocol") in ("http", "https", ""):
                    chosen = fmt
                    break
        if not chosen:
            for fmt in formats:
                if fmt.get("protocol") in ("http", "https", ""):
                    chosen = fmt
                    break

        direct_url = None
        http_headers = None
        cookies = None
        if chosen and use_direct_proxy:
            direct_url = chosen.get("url")
            fmt_headers = chosen.get("http_headers") or global_headers
            try:
                calc_headers = ydl_manager.calc_headers(chosen, load_cookies=True, proxy=proxy, impersonate=impersonate)
                http_headers = {k: v for k, v in calc_headers.items() if k.lower() != "cookie"}
                cookies = calc_headers.get("Cookie") or calc_headers.get("cookie")
            except Exception:
                http_headers = fmt_headers

        key = _get_download_cache().create_session(
            url=url,
            type="video",
            quality=quality.replace("p", ""),
            direct_url=direct_url,
            http_headers=http_headers,
            cookies=cookies,
            platform=platform,
            title=title,
            proxy=proxy,
            impersonate=impersonate,
        )
        links[quality] = f"/download?key={key}"
    return links


def _generate_photo_links(url: str, info: Dict[str, Any]) -> list:
    photos = []
    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        for idx, entry in enumerate(entries, 1):
            formats = entry.get("formats", [])
            photo_url = _get_photo_url(formats)
            key = _get_download_cache().create_session(url=url, type="photo", photo_index=idx)
            photos.append({
                "index": idx,
                "width": entry.get("width"),
                "height": entry.get("height"),
                "url": photo_url,
                "download_link": f"/download?key={key}",
            })
    else:
        formats = info.get("formats", [])
        photo_url = _get_photo_url(formats)
        key = _get_download_cache().create_session(url=url, type="photo", photo_index=1)
        photos.append({
            "index": 1,
            "width": info.get("width"),
            "height": info.get("height"),
            "url": photo_url,
            "download_link": f"/download?key={key}",
        })
    return photos


def _fetch(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    if not url:
        raise ValueError("Missing required param: url")

    proxy = params.get("proxy")
    impersonate = params.get("impersonate")
    force_ipv6 = bool(params.get("force_ipv6", False))
    info = ydl_manager.extract_info(
        url,
        proxy=proxy,
        impersonate=impersonate,
        force_ipv6=force_ipv6,
    )
    if not info:
        raise ValueError("Could not extract video info")

    content_type = _detect_content_type(info)
    platform = (info.get("extractor_key") or "unknown").lower()
    info["__fetch_proxy"] = proxy
    info["__fetch_impersonate"] = impersonate

    response = {
        "type": content_type,
        "platform": platform,
        "extract_source": _extract_source(info, platform),
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "expires_in": 300,
    }

    if content_type == "video":
        qualities = _get_available_qualities(info.get("formats", []))
        response["download_links"] = {
            "video": _generate_video_links(url, info, qualities, platform),
            "mp3": _generate_mp3_link(
                url=url,
                platform=platform,
                title=info.get("title"),
                proxy=proxy,
                impersonate=impersonate,
            ),
        }
    elif content_type == "photos":
        response["photos"] = _generate_photo_links(url, info)
    elif content_type == "playlist":
        response["playlist_count"] = info.get("playlist_count")
        response["entries"] = [
            {
                "index": i,
                "title": entry.get("title"),
                "duration": entry.get("duration"),
            }
            for i, entry in enumerate(info.get("entries", []), 1)
        ]
    return response


def _is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url or "douyin.com" in url


def _normalize_tiktok_url(url: str) -> str:
    normalized = (url or "").strip()
    if normalized.startswith("httpsr://"):
        normalized = "https://" + normalized[len("httpsr://"):]
    elif normalized.startswith("httpr://"):
        normalized = "http://" + normalized[len("httpr://"):]
    elif "://" not in normalized and _is_tiktok_url(normalized):
        normalized = f"https://{normalized.lstrip('/')}"
    return normalized


def _validate_tiktok_url(url: str) -> str:
    from urllib.parse import urlparse

    normalized = _normalize_tiktok_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("TikTok URL must use http or https")
    if not parsed.netloc or not _is_tiktok_url(normalized):
        raise ValueError("Only TikTok and Douyin URLs are supported")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    # Accept only content/post URLs and short links. Reject static pages such as /hk/about.
    is_post_path = (
        "/video/" in path
        or "/photo/" in path
        or path.startswith("/t/")
        or path.startswith("/v/")
        or path.startswith("/embed/")
    )
    is_shortlink_host = (
        host.startswith("vm.tiktok.com")
        or host.startswith("vt.tiktok.com")
        or host.startswith("v.douyin.com")
    )
    if not is_post_path and not (is_shortlink_host and path not in {"", "/"}):
        raise ValueError(
            "TikTok URL must point to a video/photo post (e.g. /@user/video/<id>), "
            "not site pages like /about"
        )
    return normalized


def _is_photo_content(info: Dict[str, Any]) -> bool:
    formats = info.get("formats", [])
    return any(fmt.get("format_id", "").startswith("image-") for fmt in formats)


def _extract_author_info(info: Dict[str, Any]) -> Dict[str, Any]:
    thumbnails = info.get("thumbnails", [])
    avatar_url = thumbnails[0].get("url", "") if thumbnails else ""
    nickname = info.get("uploader") or info.get("channel") or "unknown"
    return {
        "nickname": nickname,
        "uniqueId": info.get("uploader_id") or info.get("uploader") or "unknown",
        "signature": info.get("description", ""),
        "avatar": avatar_url,
        "avatarThumb": avatar_url,
        "avatarMedium": avatar_url,
        "avatarLarger": avatar_url,
    }


def _extract_statistics(info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "play_count": info.get("view_count", 0),
        "digg_count": info.get("like_count", 0),
        "comment_count": info.get("comment_count", 0),
        "share_count": info.get("repost_count", 0),
    }


def _extract_format_headers(
    fmt: Dict[str, Any],
    info: Dict[str, Any],
    url: Optional[str] = None,
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
) -> tuple[Dict[str, str], Optional[str]]:
    global_headers = info.get("http_headers", {})
    fmt_headers = fmt.get("http_headers", {})
    headers = {**global_headers, **fmt_headers}

    # Prefer calc_headers first to preserve slot/session affinity through
    # pre-attached __ydl_headers when ydl pooling is enabled.
    cookies = None
    try:
        calc_headers = ydl_manager.calc_headers(
            fmt,
            load_cookies=True,
            proxy=proxy,
            impersonate=impersonate,
        )
        cookies = calc_headers.get("Cookie") or calc_headers.get("cookie")
        for k, v in calc_headers.items():
            if k.lower() not in ("cookie", "accept-encoding"):
                headers[k] = v
    except Exception:
        pass

    if not cookies:
        fmt_url = fmt.get("url") or url
        if fmt_url:
            try:
                cookiejar = ydl_manager.get_cookiejar(proxy=proxy, impersonate=impersonate)
                if cookiejar and hasattr(cookiejar, "get_cookie_header"):
                    cookie_header = cookiejar.get_cookie_header(fmt_url)
                    if cookie_header:
                        cookies = cookie_header
            except Exception:
                pass

    safe_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    return safe_headers, cookies


def _generate_tiktok_video_links(
    info: Dict[str, Any],
    author: Dict[str, Any],
    request_url: str,
    proxy: Optional[str],
    impersonate: Optional[str],
) -> Dict[str, str]:
    formats = info.get("formats", [])
    links: Dict[str, str] = {}
    duration_seconds = int(info.get("duration", 0) or 0)

    video_formats = [
        f for f in formats
        if f.get("vcodec") and f["vcodec"] != "none"
        and f.get("acodec") and f["acodec"] != "none"
    ]
    audio_format = next(
        (f for f in formats
         if f.get("acodec") and f["acodec"] != "none"
         and (not f.get("vcodec") or f["vcodec"] == "none")),
        None
    )
    if not audio_format and video_formats:
        audio_format = video_formats[0]

    video_formats.sort(key=lambda x: (x.get("height", 0) * x.get("width", 0)), reverse=True)
    download_format = next((f for f in formats if f.get("format_id") == "download"), None)
    hd_formats = [f for f in video_formats if f.get("height", 0) >= 720]
    sd_formats = [f for f in video_formats if f.get("height", 0) < 720]

    if download_format:
        http_headers, cookies = _extract_format_headers(download_format, info, request_url, proxy, impersonate)
        key = _get_download_cache().create_session(
            url=request_url,
            type="video",
            quality="watermark",
            direct_url=download_format.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author["nickname"],
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=download_format.get("filesize") or download_format.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["watermark"] = f"/tiktok/download?key={key}"

    if sd_formats:
        fmt = sd_formats[0]
        http_headers, cookies = _extract_format_headers(fmt, info, request_url, proxy, impersonate)
        key = _get_download_cache().create_session(
            url=request_url,
            type="video",
            quality="no_watermark",
            direct_url=fmt.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author["nickname"],
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["no_watermark"] = f"/tiktok/download?key={key}"

    if hd_formats:
        fmt = hd_formats[0]
        http_headers, cookies = _extract_format_headers(fmt, info, request_url, proxy, impersonate)
        key = _get_download_cache().create_session(
            url=request_url,
            type="video",
            quality="no_watermark_hd",
            direct_url=fmt.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author["nickname"],
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["no_watermark_hd"] = f"/tiktok/download?key={key}"

    if audio_format:
        http_headers, cookies = _extract_format_headers(audio_format, info, request_url, proxy, impersonate)
        key = _get_download_cache().create_session(
            url=request_url,
            type="mp3",
            direct_url=audio_format.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author["nickname"],
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=audio_format.get("filesize") or audio_format.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["mp3"] = f"/tiktok/download?key={key}"

    return links


def _generate_tiktok_photo_links(
    info: Dict[str, Any],
    author: Dict[str, Any],
    request_url: str,
    proxy: Optional[str],
    impersonate: Optional[str],
) -> tuple[Dict[str, Any], list, Optional[str]]:
    formats = info.get("formats", [])
    image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
    audio_format = next((f for f in formats if f.get("format_id") == "audio"), None)
    duration_seconds = int(info.get("duration", 0) or 0)

    photos = []
    photo_keys = []
    for i, img in enumerate(image_formats):
        img_url = img.get("url")
        http_headers, cookies = _extract_format_headers(img, info, request_url, proxy, impersonate)
        key = _get_download_cache().create_session(
            url=img_url,
            type="photo",
            photo_index=i + 1,
            direct_url=img_url,
            http_headers=http_headers,
            cookies=cookies,
            author=author["nickname"],
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=img.get("filesize") or img.get("filesize_approx"),
            duration=duration_seconds,
        )
        download_link = f"/tiktok/download?key={key}"
        photo_keys.append(download_link)
        photos.append({
            "type": "photo",
            "url": img_url,
            "download_link": download_link,
        })

    links: Dict[str, Any] = {"no_watermark": photo_keys}

    if audio_format:
        http_headers, cookies = _extract_format_headers(audio_format, info, request_url, proxy, impersonate)
        key = _get_download_cache().create_session(
            url=audio_format.get("url"),
            type="mp3",
            direct_url=audio_format.get("url"),
            http_headers=http_headers,
            cookies=cookies,
            author=author["nickname"],
            platform="tiktok",
            proxy=proxy,
            impersonate=impersonate,
            filesize=audio_format.get("filesize") or audio_format.get("filesize_approx"),
            duration=duration_seconds,
        )
        links["mp3"] = f"/tiktok/download?key={key}"

    photo_urls = [img.get("url") for img in image_formats if img.get("url")]
    audio_url = audio_format.get("url") if audio_format else None
    slideshow_key = _get_download_cache().create_session(
        url=request_url,
        type="slideshow",
        photo_urls=photo_urls,
        audio_url=audio_url,
        author=author["nickname"],
        platform="tiktok",
        proxy=proxy,
        impersonate=impersonate,
        duration=duration_seconds or (len(photo_urls) * 4),
    )
    slideshow_link = f"/tiktok/download?key={slideshow_key}"

    return links, photos, slideshow_link


def _is_ip_blocked_message(message: str) -> bool:
    lower = message.lower()
    return any(p in lower for p in (
        "ip address is blocked",
        "ip blocked",
        "blocked by tiktok",
        "access denied",
        "captcha",
        "verify you are human",
        "verify that you are human",
    ))


def _is_rate_limited_tiktok_message(message: str) -> bool:
    lower = message.lower()
    return any(p in lower for p in (
        "rate-limited",
        "rate limited",
        "too many requests",
        "try again later",
    ))


def _map_tiktok_error(exc: BaseException) -> Dict[str, Any]:
    from core.error_mapping import is_dns_failure_message, is_geo_restricted_message

    msg = str(exc)
    if is_dns_failure_message(msg):
        return {"status": 503, "code": "UPSTREAM_DNS_FAILURE", "message": msg}
    if is_geo_restricted_message(msg):
        return {"status": 503, "code": "GEO_RESTRICTED", "message": msg}
    if _is_ip_blocked_message(msg):
        return {"status": 503, "code": "IP_BLOCKED", "message": msg}
    if _is_rate_limited_tiktok_message(msg):
        return {"status": 429, "code": "RATE_LIMITED", "message": msg}
    return {"status": 500, "code": "internal_error", "message": msg}


def _tiktok(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    proxy = params.get("proxy")
    impersonate = params.get("impersonate")

    if not url:
        raise ValueError("URL is required")
    url = _validate_tiktok_url(url)

    try:
        from core.extraction_cache import extraction_cache

        info = extraction_cache.get_or_extract(
            url=url,
            proxy=proxy,
            impersonate=impersonate,
            extract_fn=lambda: ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate),
        )
        if not info:
            raise ValueError("Could not extract video info")

        author = _extract_author_info(info)
        statistics = _extract_statistics(info)
        duration_ms = int(info.get("duration", 0) * 1000) if info.get("duration") else 0

        if _is_photo_content(info):
            links, photos, slideshow_link = _generate_tiktok_photo_links(info, author, url, proxy, impersonate)
            formats = info.get("formats", [])
            audio_format = next((f for f in formats if f.get("format_id") == "audio"), None)
            return {
                "status": "picker",
                "extract_source": _extract_source(info, "tiktok"),
                "title": info.get("title") or info.get("fulltitle", ""),
                "description": info.get("description") or info.get("title", ""),
                "statistics": statistics,
                "artist": author["nickname"],
                "cover": info.get("thumbnail", ""),
                "duration": duration_ms,
                "audio": audio_format.get("url") if audio_format else None,
                "download_link": links,
                "photos": photos,
                "download_slideshow": slideshow_link,
                "author": author,
            }

        links = _generate_tiktok_video_links(info, author, url, proxy, impersonate)
        formats = info.get("formats", [])
        audio_format = next(
            (f for f in formats
             if f.get("acodec") and f["acodec"] != "none"
             and (not f.get("vcodec") or f["vcodec"] == "none")),
            None
        )
        return {
            "status": "tunnel",
            "extract_source": _extract_source(info, "tiktok"),
            "title": info.get("title") or info.get("fulltitle", ""),
            "description": info.get("description") or info.get("title", ""),
            "statistics": statistics,
            "artist": author["nickname"],
            "cover": info.get("thumbnail", ""),
            "duration": duration_ms,
            "audio": audio_format.get("url") if audio_format else None,
            "download_link": links,
            "music_duration": duration_ms,
            "author": author,
        }
    except yt_dlp.utils.DownloadError as exc:
        err = _map_tiktok_error(exc)
        raise RuntimeError(json.dumps(err))
    except ValueError:
        raise
    except Exception as exc:
        err = _map_tiktok_error(exc)
        raise RuntimeError(json.dumps(err))


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"1", "true", "yes", "on"}:
            return True
        if lower in {"0", "false", "no", "off"}:
            return False
    return default


def _safe_author(name: Optional[str]) -> str:
    base = name or "tiktok"
    cleaned = "".join(c if c.isalnum() else "_" for c in base)
    return cleaned or "tiktok"


def _build_disposition_header(filename: str, download: bool) -> str:
    from urllib.parse import quote

    disposition = "attachment" if download else "inline"
    ascii_name = filename.encode("ascii", "ignore").decode()
    utf8_name = quote(filename, safe="")

    if ascii_name == filename:
        escaped = filename.replace('"', '\\"')
        return f'{disposition}; filename="{escaped}"'
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


def _build_stream_plan_from_session(session: Any, download: bool) -> Dict[str, Any]:
    from core.config import estimate_video_size, estimate_mp3_size

    content_type = session.type
    platform = (getattr(session, "platform", None) or "").strip().lower()
    if content_type == "slideshow":
        from core.config import estimate_video_size

        author = _safe_author(getattr(session, "author", None))
        filename = f"{author}_slideshow.mp4"
        response_headers = {
            "Content-Disposition": _build_disposition_header(filename, download),
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        }
        duration = getattr(session, "duration", None)
        if not duration:
            photos = getattr(session, "photo_urls", None) or []
            duration = len(photos) * 4
        estimated_size = estimate_video_size(None, duration)
        if estimated_size and estimated_size > 0:
            response_headers["Estimated-Content-Length"] = str(estimated_size)
        return {
            "fallback_proxy": False,
            "content_type": "slideshow",
            "platform": platform,
            "media_type": "video/mp4",
            "photo_urls": getattr(session, "photo_urls", None) or [],
            "audio_url": getattr(session, "audio_url", None),
            "duration_per_image": 4,
            "response_headers": response_headers,
            "can_refresh": False,
        }

    direct_url = getattr(session, "direct_url", None)
    if not direct_url:
        raise ValueError("No direct URL available in session")

    http_headers = getattr(session, "http_headers", None) or {}
    cookies = getattr(session, "cookies", None)
    safe_headers = {
        k: v for k, v in http_headers.items()
        if k.lower() not in ("accept-encoding", "cookie")
    }
    safe_headers["Accept-Encoding"] = "identity"
    if cookies:
        safe_headers["Cookie"] = cookies

    author = _safe_author(getattr(session, "author", None))
    quality = getattr(session, "quality", None) or "video"
    photo_index = getattr(session, "photo_index", None) or 1

    media_type = "application/octet-stream"
    filename = f"{author}.bin"
    if content_type == "video":
        filename = f"{author}_{quality}.mp4"
        media_type = "video/mp4"
    elif content_type == "photo":
        filename = f"{author}_photo_{photo_index}.jpg"
        media_type = "image/jpeg"
    elif content_type == "mp3":
        filename = f"{author}.mp3"
        media_type = "audio/mpeg"

    response_headers = {
        "Content-Disposition": _build_disposition_header(filename, download),
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }

    filesize = getattr(session, "filesize", None)
    duration = getattr(session, "duration", None)
    resolved_size = int(filesize) if filesize else 0
    if resolved_size > 0:
        response_headers["Content-Length"] = str(resolved_size)
    else:
        if content_type == "video":
            est = estimate_video_size(None, duration)
            if est > 0:
                response_headers["Estimated-Content-Length"] = str(est)
        elif content_type == "mp3":
            est = estimate_mp3_size(duration)
            if est > 0:
                response_headers["Estimated-Content-Length"] = str(est)

    return {
        "fallback_proxy": False,
        "content_type": content_type,
        "platform": platform,
        "direct_url": direct_url,
        "request_headers": safe_headers,
        "response_headers": response_headers,
        "media_type": media_type,
        "can_refresh": content_type in {"video", "photo", "mp3"},
    }


def _is_progressive_protocol(protocol: Optional[str]) -> bool:
    return (protocol or "") in {"http", "https", ""}


def _codec_prefix(codec: Optional[str]) -> str:
    if not codec:
        return ""
    return str(codec).split(".", 1)[0].lower()


def _is_ios_compatible_video_codec(fmt: Dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    if not vcodec or vcodec in {"none", ""}:
        return False
    return _codec_prefix(vcodec) in {"avc1", "avc3", "h264"}


def _needs_ios_video_transcode(fmt: Dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    if not vcodec or vcodec in {"none", ""}:
        return False
    return not _is_ios_compatible_video_codec(fmt)


def _select_progressive_video_format(resolved: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # Prefer muxed video+audio progressive format.
    for fmt in resolved:
        if not _is_progressive_protocol(fmt.get("protocol")):
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        if vcodec and vcodec != "none" and acodec and acodec != "none":
            return fmt
    # Fallback: any progressive format with video.
    for fmt in resolved:
        if not _is_progressive_protocol(fmt.get("protocol")):
            continue
        vcodec = fmt.get("vcodec")
        if vcodec and vcodec != "none":
            return fmt
    # Last fallback: first resolved format.
    if resolved:
        return resolved[0]
    return None


def _hydrate_generic_session(session: Any) -> Dict[str, Any]:
    from core.config import AUDIO_FORMAT, QUALITY_FORMATS
    from core.delivery import plan_delivery

    content_type = getattr(session, "type", "")
    original_url = getattr(session, "url", None)
    if not original_url:
        raise RuntimeError(json.dumps({
            "status": 400,
            "code": "missing_original_url",
            "message": "Cannot prepare download without original URL",
        }))

    proxy = getattr(session, "proxy", None)
    impersonate = getattr(session, "impersonate", None)
    info = ydl_manager.extract_info(original_url, proxy=proxy, impersonate=impersonate)
    if not info:
        raise RuntimeError(json.dumps({
            "status": 503,
            "code": "extract_failed",
            "message": "Failed to extract media information",
        }))

    session.title = info.get("title") or getattr(session, "title", None)
    session.duration = info.get("duration") or getattr(session, "duration", None)
    session.author = (
        info.get("uploader")
        or info.get("creator")
        or info.get("channel")
        or getattr(session, "author", None)
    )

    if content_type == "photo":
        photo_index = getattr(session, "photo_index", None) or 1
        fmt = None
        if info.get("_type") == "playlist":
            entries = info.get("entries", []) or []
            if 1 <= photo_index <= len(entries):
                entry = entries[photo_index - 1] or {}
                formats = entry.get("formats", []) or []
                fmt = next((f for f in formats if f.get("format_id") == "orig"), None)
                if not fmt:
                    fmt = next((f for f in formats if f.get("format_id", "").startswith("image-") and f.get("url")), None)
                if not fmt and formats:
                    fmt = next((f for f in formats if f.get("url")), formats[-1])
        else:
            formats = info.get("formats", []) or []
            fmt = next((f for f in formats if f.get("format_id") == "orig"), None)
            if not fmt:
                fmt = next((f for f in formats if f.get("format_id", "").startswith("image-") and f.get("url")), None)
            if not fmt and formats:
                fmt = next((f for f in formats if f.get("url")), formats[-1])

        if not fmt or not fmt.get("url"):
            raise RuntimeError(json.dumps({
                "status": 404,
                "code": "not_found",
                "message": "Photo format not found",
            }))

        headers, cookies = _extract_format_headers(fmt, info, original_url, proxy, impersonate)
        session.direct_url = fmt.get("url")
        session.http_headers = headers
        session.cookies = cookies
        session.filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        return {
            "protocol": fmt.get("protocol"),
            "delivery_mode": "single_progressive",
            "needs_ffmpeg": False,
            "ffmpeg_audio_only": False,
        }

    if content_type == "mp3":
        resolved = ydl_manager.resolve_formats(info, AUDIO_FORMAT, proxy=proxy, impersonate=impersonate)
        if not resolved:
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "format_resolution_failed",
                "message": "Unable to resolve audio format",
            }))
        fmt = resolved[0]
        headers, cookies = _extract_format_headers(fmt, info, original_url, proxy, impersonate)
        session.direct_url = fmt.get("url")
        session.http_headers = headers
        session.cookies = cookies
        session.filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        protocol = fmt.get("protocol")
        delivery = plan_delivery(resolved)
        # Keep MP3 as transcoded output, but let the gateway perform the
        # download/transcode so VPN is only used during extraction.
        return {
            "protocol": protocol,
            "delivery_mode": delivery.mode,
            "needs_ffmpeg": True,
            "ffmpeg_audio_only": True,
            "use_worker_mp3": False,
        }

    if content_type == "video":
        quality = str(getattr(session, "quality", "") or "")
        quality_key = f"{quality}p" if quality.isdigit() else "1080p"
        format_str = QUALITY_FORMATS.get(quality_key, QUALITY_FORMATS["1080"])
        resolved = ydl_manager.resolve_formats(info, format_str, proxy=proxy, impersonate=impersonate)
        if not resolved:
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "format_resolution_failed",
                "message": "Unable to resolve video format",
            }))
        delivery = plan_delivery(resolved)
        # Split progressive tracks (video-only + audio-only): ask Go layer to merge via ffmpeg.
        if delivery.mode == "multi_progressive":
            video_fmt = None
            audio_fmt = None
            for fmt in resolved:
                vcodec = fmt.get("vcodec")
                acodec = fmt.get("acodec")
                if vcodec and vcodec != "none" and video_fmt is None:
                    video_fmt = fmt
                if acodec and acodec != "none" and (not vcodec or vcodec == "none") and audio_fmt is None:
                    audio_fmt = fmt
            if video_fmt and audio_fmt:
                video_headers, video_cookies = _extract_format_headers(video_fmt, info, original_url, proxy, impersonate)
                audio_headers, audio_cookies = _extract_format_headers(audio_fmt, info, original_url, proxy, impersonate)
                if audio_cookies:
                    audio_headers = dict(audio_headers)
                    audio_headers["Cookie"] = audio_cookies
                session.direct_url = video_fmt.get("url")
                session.http_headers = video_headers
                session.cookies = video_cookies
                session.filesize = (video_fmt.get("filesize") or video_fmt.get("filesize_approx") or 0) + (
                    audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0
                )
                return {
                    "protocol": video_fmt.get("protocol"),
                    "delivery_mode": "multi_progressive",
                    "needs_ffmpeg": True,
                    "ffmpeg_audio_only": False,
                    "ffmpeg_merge": True,
                    "ffmpeg_audio_url": audio_fmt.get("url"),
                    "ffmpeg_audio_headers": audio_headers,
                }

        fmt = _select_progressive_video_format(resolved)
        if not fmt or not fmt.get("url"):
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "format_resolution_failed",
                "message": "Unable to select usable video format",
            }))
        headers, cookies = _extract_format_headers(fmt, info, original_url, proxy, impersonate)
        session.direct_url = fmt.get("url")
        session.http_headers = headers
        session.cookies = cookies
        session.filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        protocol = fmt.get("protocol")
        if delivery.mode == "single_progressive":
            if _needs_ios_video_transcode(fmt):
                return {
                    "protocol": protocol,
                    "delivery_mode": "ffmpeg",
                    "needs_ffmpeg": True,
                    "ffmpeg_audio_only": False,
                }
            return {
                "protocol": protocol,
                "delivery_mode": "single_progressive",
                "needs_ffmpeg": False,
                "ffmpeg_audio_only": False,
            }
        return {
            "protocol": protocol,
            "delivery_mode": "ffmpeg",
            "needs_ffmpeg": True,
            "ffmpeg_audio_only": False,
        }

    raise RuntimeError(json.dumps({
        "status": 400,
        "code": "unsupported_type",
        "message": f"Unsupported download session type: {content_type}",
    }))


def _download_prepare(params: Dict[str, Any]) -> Dict[str, Any]:
    key = params.get("key")
    if not key:
        raise ValueError("Missing required param: key")
    download = _parse_bool(params.get("download"), True)

    session = _get_download_cache().get_session(key)
    if not session:
        raise RuntimeError(json.dumps({
            "status": 404,
            "code": "not_found",
            "message": "Download link expired or invalid",
        }))

    meta = _hydrate_generic_session(session)
    # Persist enriched session (direct_url, http_headers, cookies) back to Redis
    # so mp3_stream.py can find it when called via docker exec.
    if session.direct_url:
        _get_download_cache().update_session(key, session)
    plan = _build_stream_plan_from_session(session, download)
    plan["session_type"] = session.type
    plan["protocol"] = meta.get("protocol")
    if meta.get("delivery_mode"):
        plan["delivery_mode"] = meta.get("delivery_mode")
    plan["needs_ffmpeg"] = bool(meta.get("needs_ffmpeg"))
    plan["ffmpeg_audio_only"] = bool(meta.get("ffmpeg_audio_only"))
    plan["ffmpeg_merge"] = bool(meta.get("ffmpeg_merge"))
    plan["use_worker_mp3"] = bool(meta.get("use_worker_mp3"))
    if meta.get("ffmpeg_audio_url"):
        plan["ffmpeg_audio_url"] = meta.get("ffmpeg_audio_url")
    if meta.get("ffmpeg_audio_headers"):
        plan["ffmpeg_audio_headers"] = meta.get("ffmpeg_audio_headers")
    # FFmpeg output size is not deterministic from source filesize, so avoid
    # advertising a potentially wrong Content-Length that causes truncated
    # downloads on clients.
    if plan["needs_ffmpeg"]:
        response_headers = plan.get("response_headers")
        if isinstance(response_headers, dict):
            response_headers.pop("Content-Length", None)
    return plan


def _download_refresh(params: Dict[str, Any]) -> Dict[str, Any]:
    # Refresh path is equivalent to prepare because generic prepare re-extracts data.
    return _download_prepare(params)


def _tiktok_download_prepare(params: Dict[str, Any]) -> Dict[str, Any]:
    key = params.get("key")
    if not key:
        raise ValueError("Missing required param: key")
    download = _parse_bool(params.get("download"), True)

    session = _get_download_cache().get_session(key)
    if not session:
        raise RuntimeError(json.dumps({
            "status": 404,
            "code": "not_found",
            "message": "Download link expired or invalid",
        }))

    if session.type == "slideshow":
        original_url = session.url
        proxy = session.proxy
        impersonate = session.impersonate
        if original_url:
            try:
                info = ydl_manager.extract_info(original_url, proxy=proxy, impersonate=impersonate)
                if info:
                    formats = info.get("formats", [])
                    image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
                    audio_format = next((f for f in formats if f.get("format_id") == "audio"), None)
                    if image_formats:
                        session.photo_urls = [img.get("url") for img in image_formats if img.get("url")]
                    if audio_format and audio_format.get("url"):
                        session.audio_url = audio_format.get("url")
                    session.duration = info.get("duration") or session.duration
            except Exception as exc:
                logger.warning("Could not refresh slideshow URLs during prepare: %s", exc)

    plan = _build_stream_plan_from_session(session, download)
    plan["session_type"] = session.type
    return plan


def _tiktok_download_refresh(params: Dict[str, Any]) -> Dict[str, Any]:
    key = params.get("key")
    if not key:
        raise ValueError("Missing required param: key")
    download = _parse_bool(params.get("download"), True)

    session = _get_download_cache().get_session(key)
    if not session:
        raise RuntimeError(json.dumps({
            "status": 404,
            "code": "not_found",
            "message": "Download link expired or invalid",
        }))

    content_type = session.type
    original_url = session.url
    proxy = session.proxy
    impersonate = session.impersonate
    if not original_url:
        raise RuntimeError(json.dumps({
            "status": 400,
            "code": "missing_original_url",
            "message": "Cannot refresh URL without original session URL",
        }))

    if content_type == "slideshow":
        info = ydl_manager.extract_info(original_url, proxy=proxy, impersonate=impersonate)
        if not info:
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "refresh_failed",
                "message": "Failed to refresh TikTok slideshow URLs",
            }))
        formats = info.get("formats", [])
        image_formats = [f for f in formats if f.get("format_id", "").startswith("image-")]
        audio_format = next((f for f in formats if f.get("format_id") == "audio"), None)
        if image_formats:
            session.photo_urls = [img.get("url") for img in image_formats if img.get("url")]
        if audio_format and audio_format.get("url"):
            session.audio_url = audio_format.get("url")
        session.duration = info.get("duration") or session.duration
        return _build_stream_plan_from_session(session, download)

    if content_type not in {"video", "photo", "mp3"}:
        raise RuntimeError(json.dumps({
            "status": 400,
            "code": "unsupported_refresh",
            "message": f"Refresh not supported for content type: {content_type}",
        }))

    info = ydl_manager.extract_info(original_url, proxy=proxy, impersonate=impersonate)
    if not info:
        raise RuntimeError(json.dumps({
            "status": 503,
            "code": "refresh_failed",
            "message": "Failed to refresh TikTok URL",
        }))

    if content_type == "video":
        quality = session.quality or "video"
        formats = info.get("formats", [])
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
        if not new_fmt:
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "refresh_failed",
                "message": "Unable to resolve refreshed video format",
            }))
        new_headers, new_cookies = _extract_format_headers(new_fmt, info, original_url, proxy, impersonate)
        session.direct_url = new_fmt.get("url")
        session.http_headers = new_headers
        session.cookies = new_cookies
    elif content_type == "photo":
        photo_index = session.photo_index or 1
        image_formats = [f for f in info.get("formats", []) if f.get("format_id", "").startswith("image-")]
        if photo_index > len(image_formats):
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "refresh_failed",
                "message": "Unable to resolve refreshed photo format",
            }))
        new_fmt = image_formats[photo_index - 1]
        new_headers, new_cookies = _extract_format_headers(new_fmt, info, original_url, proxy, impersonate)
        session.direct_url = new_fmt.get("url")
        session.http_headers = new_headers
        session.cookies = new_cookies
    else:
        audio_format = next(
            (f for f in info.get("formats", [])
             if f.get("acodec") and f["acodec"] != "none"
             and (not f.get("vcodec") or f["vcodec"] == "none")),
            None
        )
        if not audio_format:
            raise RuntimeError(json.dumps({
                "status": 503,
                "code": "refresh_failed",
                "message": "Unable to resolve refreshed audio format",
            }))
        new_headers, new_cookies = _extract_format_headers(audio_format, info, original_url, proxy, impersonate)
        session.direct_url = audio_format.get("url")
        session.http_headers = new_headers
        session.cookies = new_cookies

    return _build_stream_plan_from_session(session, download)


def _extract_info(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    if not url:
        raise ValueError("Missing required param: url")
    proxy = params.get("proxy")
    impersonate = params.get("impersonate")
    info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "extractor_key": info.get("extractor_key"),
        "formats": _serialize_formats(info),
    }


def _resolve_formats(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    format_str = params.get("format")
    if not url:
        raise ValueError("Missing required param: url")
    if not format_str:
        raise ValueError("Missing required param: format")
    proxy = params.get("proxy")
    impersonate = params.get("impersonate")
    info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
    resolved = ydl_manager.resolve_formats(info, format_str, proxy=proxy, impersonate=impersonate)
    return {"formats": resolved}


def _calc_headers(params: Dict[str, Any]) -> Dict[str, Any]:
    info_dict = params.get("info_dict")
    if not isinstance(info_dict, dict):
        raise ValueError("Missing required param: info_dict")
    load_cookies = bool(params.get("load_cookies", True))
    return {"headers": ydl_manager.calc_headers(info_dict, load_cookies=load_cookies)}


def _dispatch(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if method == "ping":
        return {"pong": True}
    if method == "health":
        return {"status": "ok"}
    if method == "extract_info":
        return _extract_info(params)
    if method == "fetch":
        return _fetch(params)
    if method == "tiktok":
        return _tiktok(params)
    if method == "tiktok_download_prepare":
        return _tiktok_download_prepare(params)
    if method == "tiktok_download_refresh":
        return _tiktok_download_refresh(params)
    if method == "download_prepare":
        return _download_prepare(params)
    if method == "download_refresh":
        return _download_refresh(params)
    if method == "resolve_formats":
        return _resolve_formats(params)
    if method == "calc_headers":
        return _calc_headers(params)
    raise ValueError(f"Unknown method: {method}")


def main() -> int:
    logger.info("extractor daemon started")
    while _RUNNING:
        raw = sys.stdin.readline()
        if not raw:
            break

        raw = raw.strip()
        if not raw:
            continue

        req_id: Optional[Any] = None
        try:
            request = json.loads(raw)
            req_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}

            if not method or not isinstance(method, str):
                _write_message(_error_response(req_id, "Missing required field: method", code="bad_request", status=400))
                continue
            if not isinstance(params, dict):
                _write_message(_error_response(req_id, "Field params must be an object", code="bad_request", status=400))
                continue

            result = _dispatch(method, params)
            _write_message({"id": req_id, "ok": True, "result": result})
        except yt_dlp.utils.DownloadError as exc:
            _write_message(_error_response(req_id, str(exc), code="download_error", status=400))
        except ValueError as exc:
            _write_message(_error_response(req_id, str(exc), code="bad_request", status=400))
        except RuntimeError as exc:
            payload = None
            try:
                payload = json.loads(str(exc))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                _write_message(_error_response(
                    req_id,
                    payload.get("message", "runtime error"),
                    code=payload.get("code", "runtime_error"),
                    status=int(payload.get("status", 500)),
                ))
            else:
                _write_message(_error_response(req_id, str(exc), code="runtime_error", status=500))
        except Exception as exc:
            logger.error("Unhandled daemon error: %s\n%s", exc, traceback.format_exc())
            _write_message(_error_response(req_id, str(exc), code="internal_error", status=500))

    logger.info("extractor daemon stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
