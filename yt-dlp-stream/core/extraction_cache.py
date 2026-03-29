"""Redis-based extraction cache to avoid repeated yt-dlp extract_info calls.

Caches the full info dict from extract_info() keyed by URL+proxy hash.
Dramatically reduces rate-limit risk when multiple users request the same video.

TTL is kept short (60-120s) because format URLs inside the info dict expire.
Platform-specific TTLs account for different URL expiry windows.

Async methods (get/put/invalidate) use redis.asyncio to avoid blocking the
event loop.  health_check() stays synchronous as it is called from a
background thread that has no running event loop.

Stampede protection: when multiple concurrent requests miss the cache for
the same URL, only the first caller extracts; the rest wait up to
STAMPEDE_WAIT_SECONDS and read from the cache once it is populated.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Optional, Dict, Any, Callable, Awaitable

import redis
import redis.asyncio as aioredis

logger = logging.getLogger("ytdl_stream")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Platform-specific TTLs (seconds)
PLATFORM_TTL = {
    "tiktok": 90,
    "youtube": 180,
    "twitter": 120,
    "default": 90,
}

# How long waiters will poll for a result before doing their own extraction
STAMPEDE_WAIT_SECONDS = 8
STAMPEDE_LOCK_TTL = 35  # slightly longer than max extraction time

_KEY_PREFIX = "extract:"
_LOCK_PREFIX = "extract_lock:"


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
    """Create a deterministic cache key from URL and proxy."""
    raw = f"{url}|{proxy or ''}"
    return f"{_KEY_PREFIX}{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _make_lock_key(url: str, proxy: Optional[str] = None) -> str:
    raw = f"{url}|{proxy or ''}"
    return f"{_LOCK_PREFIX}{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


class ExtractionCache:
    """Redis-backed extraction result cache with async I/O and stampede protection."""

    def __init__(self):
        self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        self._redis_sync = redis.from_url(REDIS_URL, decode_responses=True)
        self._hits = 0
        self._misses = 0

    async def get(self, url: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get cached extraction result, or None on miss."""
        key = _make_cache_key(url, proxy)
        try:
            data = await self._redis.get(key)
            if data:
                self._hits += 1
                logger.debug(f"Extraction cache HIT: {url[:60]}...")
                return json.loads(data)
        except (aioredis.ConnectionError, json.JSONDecodeError) as e:
            logger.warning(f"Extraction cache get error: {e}")
        self._misses += 1
        return None

    async def put(self, url: str, info: Dict[str, Any], proxy: Optional[str] = None) -> bool:
        """Cache an extraction result with platform-specific TTL."""
        if not info:
            return False

        key = _make_cache_key(url, proxy)
        platform = _detect_platform(url)
        ttl = PLATFORM_TTL.get(platform, PLATFORM_TTL["default"])

        try:
            serializable = _make_serializable(info)
            data = json.dumps(serializable, default=str)
            await self._redis.setex(key, ttl, data)
            logger.debug(f"Extraction cache PUT ({platform}, TTL={ttl}s): {url[:60]}...")
            return True
        except (aioredis.ConnectionError, TypeError, ValueError) as e:
            logger.warning(f"Extraction cache put error: {e}")
            return False

    async def get_or_extract(
        self,
        url: str,
        proxy: Optional[str],
        extract_fn: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
    ) -> Optional[Dict[str, Any]]:
        """
        Stampede-safe cache-aside: returns cached result or calls extract_fn
        exactly once per URL, even under concurrent load.

        Strategy (Redis SET NX distributed lock):
        1. Cache hit → return immediately.
        2. Acquire lock (SET NX, TTL=STAMPEDE_LOCK_TTL).
           a. Lock acquired → extract, store result, release lock.
           b. Lock not acquired → wait up to STAMPEDE_WAIT_SECONDS polling
              the cache.  If still nothing after the wait, fall through to
              our own extraction (avoids indefinite starvation if the lock
              holder crashes).
        """
        cached = await self.get(url, proxy)
        if cached is not None:
            return cached

        lock_key = _make_lock_key(url, proxy)
        lock_acquired = await self._redis.set(lock_key, "1", nx=True, ex=STAMPEDE_LOCK_TTL)

        if lock_acquired:
            try:
                info = await extract_fn()
                if info:
                    await self.put(url, info, proxy)
                return info
            finally:
                await self._redis.delete(lock_key)
        else:
            deadline = time.monotonic() + STAMPEDE_WAIT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(0.4)
                cached = await self.get(url, proxy)
                if cached is not None:
                    return cached
            logger.warning(
                "Stampede wait timed out for %s, falling through to own extraction", url[:60]
            )
            return await extract_fn()

    async def invalidate(self, url: str, proxy: Optional[str] = None):
        """Remove a cached entry (e.g. after known URL expiry)."""
        key = _make_cache_key(url, proxy)
        try:
            await self._redis.delete(key)
        except aioredis.ConnectionError:
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
        """Synchronous health check for background-thread monitoring."""
        try:
            self._redis_sync.ping()
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
