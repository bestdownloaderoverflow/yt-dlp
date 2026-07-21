"""Extraction cache for POST /tiktok raw engine results.

Caches normalized extractor output (not final download links).
TTL default 1800s. Stampede protection via lock + polling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

REDIS_URL = os.environ.get("REDIS_URL", "")
EXTRACT_CACHE_TTL = int(os.environ.get("TIKTOK_EXTRACT_CACHE_TTL_SECONDS", "1800"))

LOCK_TTL_SECONDS = 35
STAMPEDE_WAIT_MS = 8000
POLL_INTERVAL_MS = 300

KEY_PREFIX = "tkdl:exinfo:"
LOCK_PREFIX = "tkdl:exlock:"

logger = logging.getLogger("gateway.extract_cache")


def _build_raw_key(url: str, options: Dict[str, Any]) -> str:
    return (
        f"{url}|{options.get('proxy') or ''}|"
        f"{options.get('cookie') or ''}|{options.get('platform') or ''}|"
        f"{options.get('version') or ''}"
    )


def _build_cache_key(url: str, options: Dict[str, Any]) -> str:
    digest = hashlib.sha256(_build_raw_key(url, options).encode()).hexdigest()
    return f"{KEY_PREFIX}{digest}"


def _build_lock_key(url: str, options: Dict[str, Any]) -> str:
    digest = hashlib.sha256(_build_raw_key(url, options).encode()).hexdigest()
    return f"{LOCK_PREFIX}{digest}"


class CacheBackend:
    async def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        raise NotImplementedError

    async def set_lock(self, key: str, ttl_seconds: int) -> bool:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class MemoryCacheBackend(CacheBackend):
    def __init__(self) -> None:
        self._store: Dict[str, str] = {}
        self._expires: Dict[str, float] = {}
        self._timers: Dict[str, asyncio.TimerHandle] = {}

    def _evict(self, key: str) -> None:
        self._store.pop(key, None)
        self._expires.pop(key, None)
        handle = self._timers.pop(key, None)
        if handle:
            handle.cancel()

    async def get(self, key: str) -> Optional[str]:
        if key not in self._store:
            return None
        if time.time() >= self._expires.get(key, 0):
            self._evict(key)
            return None
        return self._store[key]

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._evict(key)
        self._store[key] = value
        self._expires[key] = time.time() + ttl_seconds
        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(ttl_seconds, self._evict, key)

    async def set_lock(self, key: str, ttl_seconds: int) -> bool:
        if await self.get(key) is not None:
            return False
        await self.set(key, "1", ttl_seconds)
        return True

    async def delete(self, key: str) -> None:
        self._evict(key)

    async def close(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self._store.clear()
        self._expires.clear()


class RedisCacheBackend(CacheBackend):
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(
            url,
            max_connections=16,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    async def get(self, key: str) -> Optional[str]:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._client.set(key, value, ex=ttl_seconds)

    async def set_lock(self, key: str, ttl_seconds: int) -> bool:
        res = await self._client.set(key, "1", nx=True, ex=ttl_seconds)
        return bool(res)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            try:
                self._client.close()
            except Exception:
                pass


_backend: Optional[CacheBackend] = None
_use_redis = False
_lock = asyncio.Lock()


async def _get_backend() -> CacheBackend:
    global _backend, _use_redis
    if _backend is not None:
        return _backend
    async with _lock:
        if _backend is not None:
            return _backend
        if REDIS_URL:
            try:
                rb = RedisCacheBackend(REDIS_URL)
                await rb.get("__ping__")
                _backend = rb
                _use_redis = True
                logger.info("Using Redis extract cache backend")
            except Exception as exc:
                logger.warning("Redis init failed (%s), falling back to memory", exc)
                _backend = MemoryCacheBackend()
                _use_redis = False
        else:
            _backend = MemoryCacheBackend()
            _use_redis = False
            logger.info("Using in-memory extract cache (set REDIS_URL to enable Redis)")
        return _backend


def is_extract_cache_redis() -> bool:
    return _use_redis


def extract_cache_ttl() -> int:
    return EXTRACT_CACHE_TTL


async def get_or_extract(
    url: str,
    options: Dict[str, Any],
    extract_fn: Callable[[], Awaitable[Any]],
) -> Any:
    try:
        backend = await _get_backend()
    except Exception as exc:
        logger.warning("cache backend unavailable (%s), extracting directly", exc)
        return await extract_fn()

    ckey = _build_cache_key(url, options)
    lkey = _build_lock_key(url, options)

    try:
        cached = await backend.get(ckey)
        if cached:
            logger.debug("cache hit: %s", ckey[:24])
            return json.loads(cached)
    except Exception as exc:
        logger.warning("cache get error: %s", exc)

    acquired = False
    try:
        acquired = await backend.set_lock(lkey, LOCK_TTL_SECONDS)
    except Exception as exc:
        logger.warning("cache setLock error: %s", exc)

    if not acquired:
        deadline = time.monotonic() + (STAMPEDE_WAIT_MS / 1000)
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_MS / 1000)
            try:
                data = await backend.get(ckey)
                if data:
                    return json.loads(data)
            except Exception:
                break

    try:
        raw = await extract_fn()
        try:
            await backend.set(ckey, json.dumps(raw, default=str), EXTRACT_CACHE_TTL)
        except Exception as exc:
            logger.warning("cache set error: %s", exc)
        return raw
    finally:
        if acquired:
            try:
                await backend.delete(lkey)
            except Exception:
                pass


async def close_extraction_cache() -> None:
    global _backend, _use_redis
    if _backend is not None:
        try:
            await _backend.close()
        except Exception as exc:
            logger.warning("cache backend close error: %s", exc)
    _backend = None
    _use_redis = False
