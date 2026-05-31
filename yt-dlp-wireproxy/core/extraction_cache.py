"""Redis-backed cache for yt-dlp extract_info results.

Reduces redundant extractions for the same URL within a short window,
lowering latency and reducing risk of platform-side rate limiting.

TTLs are platform-specific since URL expiry differs per platform:
- TikTok CDN URLs expire quickly (~90s)
- YouTube signed URLs last longer (~180s)
- Twitter/X URLs (~120s)
- Generic fallback: 60s
"""

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Optional

import redis

logger = logging.getLogger("ytdlp_stream")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
WORKER_ID = os.getenv("WORKER_ID", "").strip()

_TIKTOK_TTL = int(os.getenv("TIKTOK_EXTRACT_CACHE_TTL_SECONDS", "90"))
_DOUYIN_TTL = int(os.getenv("DOUYIN_EXTRACT_CACHE_TTL_SECONDS", str(_TIKTOK_TTL)))
_YOUTUBE_TTL = int(os.getenv("YOUTUBE_EXTRACT_CACHE_TTL_SECONDS", "180"))
_TWITTER_TTL = int(os.getenv("TWITTER_EXTRACT_CACHE_TTL_SECONDS", "120"))

_PLATFORM_TTL: dict[str, int] = {
    "tiktok": _TIKTOK_TTL,
    "douyin": _DOUYIN_TTL,
    "youtube": _YOUTUBE_TTL,
    "twitter": _TWITTER_TTL,
    "x": _TWITTER_TTL,
}
_DEFAULT_TTL = 60

# Stampede protection: how long the distributed lock is held (seconds)
_LOCK_TTL = 35
# How long waiters poll before falling through to own extraction
_STAMPEDE_WAIT = 8
_POLL_INTERVAL = 0.3

_KEY_PREFIX = "exinfo:"
_LOCK_PREFIX = "exlock:"


def _cache_key(url: str, proxy: Optional[str], impersonate: Optional[str]) -> str:
    """SHA-256 of url|proxy|impersonate|worker_id for a stable, compact key.

    WORKER_ID is included so that each worker (with its own outgoing IP via
    gluetun/VPN) maintains a separate cache. CDN URLs returned by yt-dlp are
    IP-bound: a URL extracted on worker w2 will be rejected (403) when a
    different worker w3 tries to download it from a different IP.
    """
    raw = f"{url}|{proxy or ''}|{impersonate or ''}|{WORKER_ID}"
    return _KEY_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def _lock_key(url: str, proxy: Optional[str], impersonate: Optional[str]) -> str:
    raw = f"{url}|{proxy or ''}|{impersonate or ''}|{WORKER_ID}"
    return _LOCK_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def _ttl_for_platform(info: dict) -> int:
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    return _PLATFORM_TTL.get(extractor, _DEFAULT_TTL)


def _make_serializable(obj: Any) -> Any:
    """Recursively convert yt-dlp info dict to JSON-serializable form."""
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_serializable(i) for i in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


class ExtractionCache:
    """Redis-backed cache with stampede protection for yt-dlp info dicts."""

    def __init__(self):
        self._redis: Optional[redis.Redis] = None
        self._available = False
        self._last_check = 0.0
        self._check_interval = 30

    def _get_redis(self) -> Optional[redis.Redis]:
        now = time.time()
        if now - self._last_check < self._check_interval and self._redis is not None:
            return self._redis if self._available else None
        try:
            if self._redis is None:
                self._redis = redis.from_url(
                    REDIS_URL, decode_responses=True, socket_connect_timeout=1
                )
            self._redis.ping()
            self._available = True
        except Exception:
            self._available = False
        self._last_check = now
        return self._redis if self._available else None

    def get(self, url: str, proxy: Optional[str], impersonate: Optional[str]) -> Optional[dict]:
        """Return cached info dict or None."""
        r = self._get_redis()
        if r is None:
            return None
        try:
            data = r.get(_cache_key(url, proxy, impersonate))
            if data:
                return json.loads(data)
        except Exception as e:
            logger.debug(f"ExtractionCache.get error: {e}")
        return None

    def put(self, url: str, proxy: Optional[str], impersonate: Optional[str], info: dict) -> None:
        """Store info dict with platform-appropriate TTL."""
        r = self._get_redis()
        if r is None:
            return
        try:
            ttl = _ttl_for_platform(info)
            serializable = _make_serializable(info)
            r.setex(_cache_key(url, proxy, impersonate), ttl, json.dumps(serializable))
        except Exception as e:
            logger.debug(f"ExtractionCache.put error: {e}")

    def get_or_extract(
        self,
        url: str,
        proxy: Optional[str],
        impersonate: Optional[str],
        extract_fn: Callable[[], dict],
    ) -> dict:
        """
        Return cached info or call extract_fn, with Redis SET NX stampede protection.

        If another worker is currently extracting the same URL, wait up to
        _STAMPEDE_WAIT seconds for the result to appear in cache before
        falling through to own extraction.
        """
        # Fast path: already cached
        cached = self.get(url, proxy, impersonate)
        if cached is not None:
            logger.debug(f"ExtractionCache hit: {url[:60]}")
            return cached

        r = self._get_redis()
        if r is None:
            return extract_fn()

        lkey = _lock_key(url, proxy, impersonate)
        ckey = _cache_key(url, proxy, impersonate)

        # Try to acquire the extraction lock (SET NX)
        acquired = r.set(lkey, "1", nx=True, ex=_LOCK_TTL)

        if not acquired:
            # Another worker is extracting — wait for their result
            deadline = time.time() + _STAMPEDE_WAIT
            while time.time() < deadline:
                time.sleep(_POLL_INTERVAL)
                try:
                    data = r.get(ckey)
                    if data:
                        logger.debug(f"ExtractionCache stampede wait hit: {url[:60]}")
                        return json.loads(data)
                except Exception:
                    break
            # Timed out waiting — fall through to own extraction
            logger.debug(f"ExtractionCache stampede wait timeout: {url[:60]}")

        try:
            info = extract_fn()
            self.put(url, proxy, impersonate, info)
            return info
        finally:
            if acquired:
                try:
                    r.delete(lkey)
                except Exception:
                    pass

    def invalidate(self, url: str, proxy: Optional[str], impersonate: Optional[str]) -> None:
        """Manually invalidate a cached entry."""
        r = self._get_redis()
        if r is None:
            return
        try:
            r.delete(_cache_key(url, proxy, impersonate))
        except Exception as e:
            logger.debug(f"ExtractionCache.invalidate error: {e}")


# Singleton instance
extraction_cache = ExtractionCache()
