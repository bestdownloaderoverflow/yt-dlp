"""Redis-based extraction cache to avoid repeated yt-dlp extract_info calls.

Caches the full info dict from extract_info() keyed by URL+proxy hash.
Dramatically reduces rate-limit risk when multiple users request the same video.

TTL is kept short (60-120s) because format URLs inside the info dict expire.
Platform-specific TTLs account for different URL expiry windows.
"""

import hashlib
import json
import logging
import os
import time
from typing import Optional, Dict, Any

import redis

logger = logging.getLogger("ytdl_stream")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Platform-specific TTLs (seconds)
# TikTok URLs expire quickly (~5 min), so cache shorter
# YouTube URLs last longer (~6 hours), so cache longer
PLATFORM_TTL = {
    "tiktok": 90,
    "youtube": 180,
    "twitter": 120,
    "default": 90,
}

_KEY_PREFIX = "extract:"


def _detect_platform(url: str) -> str:
    """Detect platform from URL for TTL selection."""
    url_lower = url.lower()
    if "tiktok.com" in url_lower or "douyin.com" in url_lower:
        return "tiktok"
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    if "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    return "default"


def _make_cache_key(url: str, proxy: Optional[str] = None) -> str:
    """Create a deterministic cache key from URL and proxy.

    We hash so that long URLs / proxy strings don't bloat Redis key space.
    Proxy is included because different proxies may get different geo-results.
    """
    raw = f"{url}|{proxy or ''}"
    return f"{_KEY_PREFIX}{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


class ExtractionCache:
    """Redis-backed extraction result cache."""

    def __init__(self):
        self._redis = redis.from_url(REDIS_URL, decode_responses=True)
        self._hits = 0
        self._misses = 0

    def get(self, url: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get cached extraction result, or None on miss."""
        key = _make_cache_key(url, proxy)
        try:
            data = self._redis.get(key)
            if data:
                self._hits += 1
                logger.debug(f"Extraction cache HIT: {url[:60]}...")
                return json.loads(data)
        except (redis.ConnectionError, json.JSONDecodeError) as e:
            logger.warning(f"Extraction cache get error: {e}")
        self._misses += 1
        return None

    def put(self, url: str, info: Dict[str, Any], proxy: Optional[str] = None) -> bool:
        """Cache an extraction result with platform-specific TTL.

        Returns True if cached successfully.
        """
        if not info:
            return False

        key = _make_cache_key(url, proxy)
        platform = _detect_platform(url)
        ttl = PLATFORM_TTL.get(platform, PLATFORM_TTL["default"])

        try:
            # Serialize — strip non-serializable objects
            serializable = _make_serializable(info)
            data = json.dumps(serializable, default=str)
            self._redis.setex(key, ttl, data)
            logger.debug(f"Extraction cache PUT ({platform}, TTL={ttl}s): {url[:60]}...")
            return True
        except (redis.ConnectionError, TypeError, ValueError) as e:
            logger.warning(f"Extraction cache put error: {e}")
            return False

    def invalidate(self, url: str, proxy: Optional[str] = None):
        """Remove a cached entry (e.g. after known URL expiry)."""
        key = _make_cache_key(url, proxy)
        try:
            self._redis.delete(key)
        except redis.ConnectionError:
            pass

    @property
    def stats(self) -> Dict[str, int]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total * 100, 1) if total > 0 else 0,
        }

    def health_check(self) -> bool:
        try:
            self._redis.ping()
            return True
        except redis.ConnectionError:
            return False


def _make_serializable(obj: Any) -> Any:
    """Recursively convert info dict to JSON-serializable form.

    yt-dlp info dicts can contain non-serializable objects like
    CookieJar, format selectors, etc. We strip those.
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # Skip known non-serializable / internal keys
            if k.startswith("__") or k in ("_filename", "requested_downloads"):
                continue
            try:
                result[k] = _make_serializable(v)
            except (TypeError, ValueError):
                result[k] = str(v)
        return result
    elif isinstance(obj, (list, tuple)):
        return [_make_serializable(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)


# Singleton
extraction_cache = ExtractionCache()
