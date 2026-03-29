"""
Security utilities untuk internal endpoints.

Features:
- HMAC signature generation & validation
- Stream token dengan expiration
- Rate limiting helpers
"""
import hashlib
import hmac
import logging
import os
import time
import secrets
from typing import Optional, Dict, Tuple
from dataclasses import dataclass

import redis as _redis_lib

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


logger = logging.getLogger("uvicorn.error")


# Secret key untuk HMAC (use env in production for stable multi-worker tokens)
_env_secret = os.getenv("STREAM_SECRET_KEY")
if _env_secret:
    SECRET_KEY = _env_secret
else:
    SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "STREAM_SECRET_KEY is not set; using ephemeral in-memory secret. "
        "Internal stream tokens will be invalid after restart."
    )


@dataclass
class StreamToken:
    """Token untuk secure stream access."""
    stream_id: str
    expires_at: float
    signature: str
    metadata: Optional[Dict] = None


def generate_hmac(data: str, secret: str = SECRET_KEY) -> str:
    """Generate HMAC-SHA256 signature."""
    return hmac.new(
        secret.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()


def verify_hmac(data: str, signature: str, secret: str = SECRET_KEY) -> bool:
    """Verify HMAC signature."""
    expected = generate_hmac(data, secret)
    return hmac.compare_digest(expected, signature)


def create_stream_token(
    stream_id: str,
    ttl: int = 3600,
    metadata: Optional[Dict] = None
) -> StreamToken:
    """
    Create secure stream token dengan expiration.
    
    Args:
        stream_id: Unique stream identifier
        ttl: Time-to-live in seconds (default 1 hour)
        metadata: Optional metadata to include
        
    Returns:
        StreamToken dengan signature
    """
    expires_at = time.time() + ttl
    
    # Generate signature dari stream_id + expiration
    data = f"{stream_id}:{expires_at}"
    signature = generate_hmac(data)
    
    return StreamToken(
        stream_id=stream_id,
        expires_at=expires_at,
        signature=signature,
        metadata=metadata
    )


def validate_stream_token(
    stream_id: str,
    expires_at: float,
    signature: str
) -> Tuple[bool, Optional[str]]:
    """
    Validate stream token.
    
    Returns:
        (is_valid, error_message)
    """
    # Check expiration
    if time.time() > expires_at:
        return False, "Token expired"
    
    # Verify signature
    data = f"{stream_id}:{expires_at}"
    if not verify_hmac(data, signature):
        return False, "Invalid signature"
    
    return True, None


def is_localhost(host: str) -> bool:
    """Check if request is from localhost."""
    return host in ("127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1")


def generate_stream_id() -> str:
    """Generate secure random stream ID."""
    return secrets.token_urlsafe(16)


class RateLimiter:
    """
    Distributed Redis-backed rate limiter using a fixed-window counter.

    Shared across all Granian worker processes via Redis so the configured
    limit is honoured globally, not per-process.  Falls back to in-memory
    sliding-window counting when Redis is unavailable.
    """

    def __init__(self, max_requests: int = 100, window: int = 60):
        """
        Args:
            max_requests: Max requests per window
            window: Time window in seconds
        """
        self.max_requests = max_requests
        self.window = window
        self._redis: Optional[_redis_lib.Redis] = None
        self._local_fallback: Dict[str, list] = {}
        self._connect_redis()

    def _connect_redis(self) -> None:
        try:
            self._redis = _redis_lib.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=1,
            )
            self._redis.ping()
        except Exception as e:
            logger.warning(f"RateLimiter: Redis unavailable, falling back to in-memory: {e}")
            self._redis = None

    def is_allowed(self, client_id: str) -> bool:
        """Check if client is allowed to make request."""
        if self._redis is not None:
            try:
                return self._is_allowed_redis(client_id)
            except Exception as e:
                logger.warning(f"RateLimiter Redis error, using fallback: {e}")
                self._redis = None
        return self._is_allowed_local(client_id)

    def _is_allowed_redis(self, client_id: str) -> bool:
        """Fixed-window counter via atomic INCR + EXPIRE."""
        key = f"rl:{client_id}"
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self.window)
        count = pipe.execute()[0]
        return count <= self.max_requests

    def _is_allowed_local(self, client_id: str) -> bool:
        """In-memory sliding-window fallback."""
        now = time.time()
        if client_id in self._local_fallback:
            self._local_fallback[client_id] = [
                ts for ts in self._local_fallback[client_id]
                if now - ts < self.window
            ]
        requests = self._local_fallback.get(client_id, [])
        if len(requests) >= self.max_requests:
            return False
        if client_id not in self._local_fallback:
            self._local_fallback[client_id] = []
        self._local_fallback[client_id].append(now)
        return True

    def cleanup_old_entries(self):
        """Remove expired in-memory entries (no-op when Redis is active)."""
        if self._redis is not None:
            return
        now = time.time()
        to_remove = []
        for client_id, timestamps in self._local_fallback.items():
            valid = [ts for ts in timestamps if now - ts < self.window]
            if not valid:
                to_remove.append(client_id)
            else:
                self._local_fallback[client_id] = valid
        for client_id in to_remove:
            del self._local_fallback[client_id]


# Global rate limiter instance
rate_limiter = RateLimiter(max_requests=100, window=60)
