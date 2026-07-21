"""Session store for /tiktok/download keys.

In-memory backend with optional Redis. TTL 300s.
Key prefix: tkdl:session:
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

DEFAULT_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "300"))
REDIS_KEY_PREFIX = "tkdl:session:"
REDIS_URL = os.environ.get("REDIS_URL", "")

logger = logging.getLogger("gateway.session")


class SessionBackend:
    async def create(self, data: Dict[str, Any], ttl_seconds: int) -> str:
        raise NotImplementedError

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def claim(self, key: str) -> Optional[Dict[str, Any]]:
        """Atomic get-and-delete for one-shot download keys."""
        session = await self.get(key)
        if session is not None:
            await self.delete(key)
        return session

    async def count(self) -> int:
        raise NotImplementedError

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        raise NotImplementedError


class MemoryBackend(SessionBackend):
    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._timers: Dict[str, asyncio.TimerHandle] = {}
        self._lock = asyncio.Lock()

    async def create(self, data: Dict[str, Any], ttl_seconds: int) -> str:
        key = _generate_key()
        now = time.time()
        session = {**data, "key": key, "createdAt": now, "expiresAt": now + ttl_seconds}
        async with self._lock:
            self._sessions[key] = session
            loop = asyncio.get_running_loop()
            self._timers[key] = loop.call_later(ttl_seconds, self._evict, key)
        return key

    def _evict(self, key: str) -> None:
        self._sessions.pop(key, None)
        handle = self._timers.pop(key, None)
        if handle:
            handle.cancel()

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        session = self._sessions.get(key)
        if not session:
            return None
        if time.time() >= session.get("expiresAt", 0):
            self._evict(key)
            return None
        return session

    async def delete(self, key: str) -> None:
        self._evict(key)

    async def claim(self, key: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            session = await self.get(key)
            if session is not None:
                self._evict(key)
            return session

    async def count(self) -> int:
        return len(self._sessions)

    async def close(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self._sessions.clear()


class RedisBackend(SessionBackend):
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(
            url,
            max_connections=16,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_error=[aioredis.ConnectionError, aioredis.TimeoutError],
        )
        self._url = url

    def _rkey(self, key: str) -> str:
        return f"{REDIS_KEY_PREFIX}{key}"

    async def create(self, data: Dict[str, Any], ttl_seconds: int) -> str:
        key = _generate_key()
        now = time.time()
        session = {**data, "key": key, "createdAt": now, "expiresAt": now + ttl_seconds}
        await self._client.set(self._rkey(key), json.dumps(session), ex=ttl_seconds)
        return key

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        raw = await self._client.get(self._rkey(key))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            await self._client.delete(self._rkey(key))
            return None

    async def delete(self, key: str) -> None:
        await self._client.delete(self._rkey(key))

    async def claim(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        raw = await self._client.getdel(self._rkey(key))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def count(self) -> int:
        count = 0
        async for _ in self._client.scan_iter(match=f"{REDIS_KEY_PREFIX}*", count=200):
            count += 1
        return count

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception as exc:
            logger.warning("Redis ping failed: %s", exc)
            return False

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            try:
                self._client.close()
            except Exception:
                pass


_backend: Optional[SessionBackend] = None
_backend_is_redis = False
_backend_lock = asyncio.Lock()


async def _get_backend() -> SessionBackend:
    global _backend, _backend_is_redis
    if _backend is not None:
        return _backend
    async with _backend_lock:
        if _backend is not None:
            return _backend
        if REDIS_URL:
            try:
                rb = RedisBackend(REDIS_URL)
                if await rb.ping():
                    _backend = rb
                    _backend_is_redis = True
                    logger.info("Using Redis session backend: %s", _mask_url(REDIS_URL))
                else:
                    raise ConnectionError("ping failed")
            except Exception as exc:
                logger.warning("Redis init failed (%s), falling back to memory", exc)
                _backend = MemoryBackend()
                _backend_is_redis = False
        else:
            _backend = MemoryBackend()
            _backend_is_redis = False
            logger.info("Using in-memory session backend (set REDIS_URL to enable Redis)")
        return _backend


def _mask_url(url: str) -> str:
    return re.sub(r":[^@]+@", ":***@", url)


def _generate_key() -> str:
    return uuid.uuid4().hex[:22]


async def create_session(
    data: Dict[str, Any], ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> str:
    try:
        backend = await _get_backend()
        return await backend.create(data, ttl_seconds)
    except Exception as exc:
        logger.warning("session.create failed (%s), using memory fallback", exc)
        mem = MemoryBackend()
        return await mem.create(data, ttl_seconds)


async def get_session(key: str) -> Optional[Dict[str, Any]]:
    try:
        backend = await _get_backend()
        return await backend.get(key)
    except Exception as exc:
        logger.warning("session.get failed (%s)", exc)
        return None


async def claim_session(key: str) -> Optional[Dict[str, Any]]:
    try:
        backend = await _get_backend()
        return await backend.claim(key)
    except Exception as exc:
        logger.warning("session.claim failed (%s)", exc)
        return None


async def restore_session(
    data: Dict[str, Any], ttl_seconds: Optional[int] = None
) -> str:
    """Re-store a claimed session (e.g. after stream failure before headers)."""
    remaining = ttl_seconds
    if remaining is None:
        expires = data.get("expiresAt")
        if expires:
            remaining = max(1, int(expires - time.time()))
        else:
            remaining = DEFAULT_TTL_SECONDS
    payload = {k: v for k, v in data.items() if k not in ("key", "createdAt", "expiresAt")}
    return await create_session(payload, remaining)


async def delete_session(key: str) -> None:
    try:
        backend = await _get_backend()
        await backend.delete(key)
    except Exception:
        pass


async def active_session_count() -> int:
    try:
        backend = await _get_backend()
        return await backend.count()
    except Exception:
        return 0


async def redis_ping() -> bool:
    if not _backend_is_redis or _backend is None:
        return True
    if hasattr(_backend, "ping"):
        return await _backend.ping()
    return True


async def close_session_store() -> None:
    global _backend, _backend_is_redis
    if _backend is not None:
        try:
            await _backend.close()
        except Exception as exc:
            logger.warning("session backend close error: %s", exc)
    _backend = None
    _backend_is_redis = False


def is_redis_backend() -> bool:
    return _backend_is_redis
