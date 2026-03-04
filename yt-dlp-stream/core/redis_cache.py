"""Redis cache for download session management."""

import json
import os
import uuid
from dataclasses import dataclass, asdict
from typing import Optional

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEFAULT_TTL = 300  # 5 minutes


@dataclass
class DownloadSession:
    """Download session data."""

    url: str
    type: str  # video | mp3 | photo
    format_id: Optional[str] = None
    quality: Optional[str] = None
    photo_index: Optional[int] = None
    created_at: Optional[float] = None
    # Fields for direct download proxy (Twitter/X session integrity)
    direct_url: Optional[str] = None
    http_headers: Optional[dict] = None
    cookies: Optional[str] = None


class RedisDownloadCache:
    """Redis-based cache for download sessions with TTL."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL):
        self._redis = redis.from_url(REDIS_URL, decode_responses=True)
        self._ttl = ttl_seconds
        self._key_prefix = "download:"

    def create_session(
        self,
        url: str,
        type: str,
        format_id: str = None,
        quality: str = None,
        photo_index: int = None,
        direct_url: str = None,
        http_headers: dict = None,
        cookies: str = None,
    ) -> str:
        """Create new session and return key."""
        key = str(uuid.uuid4())
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
        )

        redis_key = f"{self._key_prefix}{key}"
        self._redis.setex(redis_key, self._ttl, json.dumps(asdict(session)))
        return key

    def get_session(self, key: str) -> Optional[DownloadSession]:
        """Get session if valid, else None."""
        redis_key = f"{self._key_prefix}{key}"
        data = self._redis.get(redis_key)

        if not data:
            return None

        session_dict = json.loads(data)
        return DownloadSession(**session_dict)

    def delete_session(self, key: str):
        """Delete session manually."""
        redis_key = f"{self._key_prefix}{key}"
        self._redis.delete(redis_key)

    def health_check(self) -> bool:
        """Check Redis connection."""
        try:
            self._redis.ping()
            return True
        except redis.ConnectionError:
            return False


# Singleton instance
download_cache = RedisDownloadCache()
