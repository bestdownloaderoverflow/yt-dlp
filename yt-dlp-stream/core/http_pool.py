"""Shared httpx connection pool for streaming responses.

Reuses TCP+TLS connections to CDN hosts across requests, reducing
latency by ~150-250ms per request and lowering file descriptor usage.
"""

import logging
from typing import Optional

import httpx

from core.config import STREAM_BUFFER_SIZE

logger = logging.getLogger("ytdl_stream")

# App-level singleton async client with connection pooling
_async_pool: Optional[httpx.AsyncClient] = None

# Sync client for blocking contexts (e.g. slideshow_generator)
_sync_pool: Optional[httpx.Client] = None


def get_async_pool() -> httpx.AsyncClient:
    """Get or create the shared async httpx client pool."""
    global _async_pool
    if _async_pool is None or _async_pool.is_closed:
        _async_pool = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout=300, connect=10),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )
        logger.info("Created shared async httpx connection pool")
    return _async_pool


def get_sync_pool() -> httpx.Client:
    """Get or create the shared sync httpx client pool."""
    global _sync_pool
    if _sync_pool is None or _sync_pool.is_closed:
        _sync_pool = httpx.Client(
            follow_redirects=True,
            timeout=60,
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
        )
        logger.info("Created shared sync httpx connection pool")
    return _sync_pool


async def close_async_pool():
    """Close the async pool (call on app shutdown)."""
    global _async_pool
    if _async_pool and not _async_pool.is_closed:
        await _async_pool.aclose()
        _async_pool = None
        logger.info("Closed shared async httpx connection pool")


def close_sync_pool():
    """Close the sync pool (call on app shutdown)."""
    global _sync_pool
    if _sync_pool and not _sync_pool.is_closed:
        _sync_pool.close()
        _sync_pool = None
        logger.info("Closed shared sync httpx connection pool")
