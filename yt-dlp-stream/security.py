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
import threading
import time
import secrets
from typing import Optional, Dict, Tuple
from dataclasses import dataclass


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
    Distributed rate limiter menggunakan Redis fixed-window counter.
    Fallback ke in-memory sliding window jika Redis tidak tersedia.
    Konsisten antar multiple workers/processes.
    """

    def __init__(self, max_requests: int = 100, window: int = 60):
        """
        Args:
            max_requests: Max requests per window
            window: Time window in seconds
        """
        self.max_requests = max_requests
        self.window = window
        # In-memory fallback
        self._fallback_requests: Dict[str, list] = {}
        self._fallback_lock = threading.Lock()
        # Redis client (lazy init)
        self._redis = None
        self._redis_available = False
        self._redis_check_lock = threading.Lock()
        self._last_redis_check = 0.0
        self._redis_check_interval = 30  # Re-check Redis availability every 30s

        # Start background cleanup thread for in-memory fallback dict
        self._cleanup_thread = threading.Thread(target=self._run_cleanup, daemon=True, name="RateLimiterCleanup")
        self._cleanup_thread.start()

    def _get_redis(self):
        """Lazy-init Redis client, re-check availability periodically."""
        now = time.time()
        with self._redis_check_lock:
            if now - self._last_redis_check < self._redis_check_interval and self._redis is not None:
                return self._redis if self._redis_available else None
            try:
                import redis as _redis_lib
                redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
                if self._redis is None:
                    self._redis = _redis_lib.from_url(
                        redis_url, decode_responses=True, socket_connect_timeout=1
                    )
                self._redis.ping()
                self._redis_available = True
            except Exception:
                self._redis_available = False
            self._last_redis_check = now
        return self._redis if self._redis_available else None

    def is_allowed(self, client_id: str) -> bool:
        """Check if client is allowed to make request."""
        r = self._get_redis()
        if r is not None:
            return self._is_allowed_redis(r, client_id)
        return self._is_allowed_memory(client_id)

    def _is_allowed_redis(self, r, client_id: str) -> bool:
        """Redis fixed-window counter using atomic INCR + EXPIRE pipeline."""
        try:
            # Use a fixed window key per minute/window slot
            slot = int(time.time() // self.window)
            key = f"rl:{client_id}:{slot}"
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.window * 2)  # TTL double window as safety
            results = pipe.execute()
            count = results[0]
            return count <= self.max_requests
        except Exception:
            # Redis error mid-request — fallback to in-memory
            self._redis_available = False
            return self._is_allowed_memory(client_id)

    def _is_allowed_memory(self, client_id: str) -> bool:
        """In-memory sliding window fallback."""
        now = time.time()
        with self._fallback_lock:
            if client_id in self._fallback_requests:
                self._fallback_requests[client_id] = [
                    ts for ts in self._fallback_requests[client_id]
                    if now - ts < self.window
                ]
                if not self._fallback_requests[client_id]:
                    del self._fallback_requests[client_id]
            requests = self._fallback_requests.get(client_id, [])
            if len(requests) >= self.max_requests:
                return False
            if client_id not in self._fallback_requests:
                self._fallback_requests[client_id] = []
            self._fallback_requests[client_id].append(now)
        return True

    def cleanup_old_entries(self):
        """Remove expired in-memory fallback entries."""
        now = time.time()
        with self._fallback_lock:
            to_remove = []
            for client_id, timestamps in self._fallback_requests.items():
                valid = [ts for ts in timestamps if now - ts < self.window]
                if not valid:
                    to_remove.append(client_id)
                else:
                    self._fallback_requests[client_id] = valid
            for client_id in to_remove:
                del self._fallback_requests[client_id]

    def _run_cleanup(self):
        """Background thread to periodically clean up fallback entries."""
        import time
        while True:
            time.sleep(60)
            try:
                self.cleanup_old_entries()
            except Exception:
                pass


# Global rate limiter instance
rate_limiter = RateLimiter(max_requests=100, window=60)
