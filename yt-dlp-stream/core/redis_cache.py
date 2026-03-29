"""Redis cache for download session management."""

import json
import os
import re
import uuid
from dataclasses import dataclass, asdict
from typing import Optional

import redis
import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEFAULT_TTL = 300  # 5 minutes
WORKER_ID = os.getenv("WORKER_ID", "").strip()
PROXY_ID_RE = re.compile(r"gluetun-(\d+)")


@dataclass
class DownloadSession:
    """Download session data."""

    url: str
    type: str  # video | mp3 | photo | slideshow
    format_id: Optional[str] = None
    quality: Optional[str] = None
    photo_index: Optional[int] = None
    created_at: Optional[float] = None
    # Fields for direct download proxy (Twitter/X session integrity)
    direct_url: Optional[str] = None
    http_headers: Optional[dict] = None
    cookies: Optional[str] = None
    # TikTok-specific fields
    photo_urls: Optional[list] = None  # List of photo URLs for slideshow
    audio_url: Optional[str] = None  # Audio URL for slideshow
    author: Optional[str] = None  # Author nickname for filename
    platform: Optional[str] = None  # Platform identifier (tiktok, etc)
    title: Optional[str] = None  # Original title for filename
    proxy: Optional[str] = None  # Request-level proxy used during fetch
    impersonate: Optional[str] = None  # Request-level impersonation used during fetch
    filesize: Optional[int] = None  # Exact/approx media size if known
    duration: Optional[int] = None  # Duration in seconds if known


class RedisDownloadCache:
    """Redis-based cache for download sessions with TTL."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL):
        self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        self._redis_sync = redis.from_url(REDIS_URL, decode_responses=True)
        self._ttl = ttl_seconds
        self._key_prefix = "download:"

    def _key_affinity_prefix(self, proxy: Optional[str]) -> str:
        """Prefer proxy affinity so download can stay on the same VPN egress."""
        if proxy:
            match = PROXY_ID_RE.search(proxy)
            if match:
                return f"p{match.group(1)}"
        if WORKER_ID:
            return WORKER_ID
        return ""

    def _raw_key_from_public_key(self, key: str) -> str:
        """Strip optional public affinity prefix from session key."""
        if not key or "-" not in key:
            return key
        prefix, raw_key = key.split("-", 1)
        if prefix.startswith(("w", "p")):
            return raw_key
        return key

    async def create_session(
        self,
        url: str,
        type: str,
        format_id: str = None,
        quality: str = None,
        photo_index: int = None,
        direct_url: str = None,
        http_headers: dict = None,
        cookies: str = None,
        photo_urls: list = None,
        audio_url: str = None,
        author: str = None,
        platform: str = None,
        title: str = None,
        proxy: str = None,
        impersonate: str = None,
        filesize: int = None,
        duration: int = None,
    ) -> str:
        """Create new session and return key."""
        raw_key = str(uuid.uuid4())
        affinity_prefix = self._key_affinity_prefix(proxy)
        key = f"{affinity_prefix}-{raw_key}" if affinity_prefix else raw_key
        session = DownloadSession(
            url=url,
            type=type,
            format_id=format_id,
            quality=quality,
            photo_index=photo_index,
            created_at=None,  # Redis akan handle timestamp via TTL
            direct_url=direct_url,
            http_headers=http_headers,
            cookies=cookies,
            photo_urls=photo_urls,
            audio_url=audio_url,
            author=author,
            platform=platform,
            title=title,
            proxy=proxy,
            impersonate=impersonate,
            filesize=filesize,
            duration=duration,
        )

        redis_key = f"{self._key_prefix}{raw_key}"
        await self._redis.setex(redis_key, self._ttl, json.dumps(asdict(session)))
        return key

    async def get_session(self, key: str) -> Optional[DownloadSession]:
        """Get session if valid, else None."""
        raw_key = self._raw_key_from_public_key(key)
        redis_key = f"{self._key_prefix}{raw_key}"
        data = await self._redis.get(redis_key)

        if not data:
            return None

        session_dict = json.loads(data)
        return DownloadSession(**session_dict)

    async def delete_session(self, key: str):
        """Delete session manually."""
        raw_key = self._raw_key_from_public_key(key)
        redis_key = f"{self._key_prefix}{raw_key}"
        await self._redis.delete(redis_key)

    def health_check(self) -> bool:
        """Synchronous health check for background-thread monitoring."""
        try:
            self._redis_sync.ping()
            return True
        except redis.ConnectionError:
            return False


# Singleton instance
download_cache = RedisDownloadCache()
