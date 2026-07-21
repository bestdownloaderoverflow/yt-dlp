"""FastAPI application factory for TikTokDownloader gateway."""

from __future__ import annotations

import datetime
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from gateway import extraction_cache
from gateway import session as session_store
from gateway.engine_adapter import close_engine, start_engine
from gateway.routes import register_routes
from gateway import __version__

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("gateway.app")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
IS_PROD = ENVIRONMENT == "production"
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(64 * 1024)))
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")
DEFAULT_VERSION = os.environ.get("DEFAULT_VERSION", "v1")

_metrics = {
    "requests_total": 0,
    "tiktok_post_total": 0,
    "douyin_post_total": 0,
    "tiktok_download_total": 0,
    "tiktok_post_errors": 0,
    "douyin_post_errors": 0,
    "tiktok_download_errors": 0,
}


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_BODY_BYTES:
            return StarletteResponse(
                content='{"error":"Request body too large"}',
                media_type="application/json",
                status_code=413,
            )
        return await call_next(request)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        _metrics["requests_total"] = _metrics.get("requests_total", 0) + 1
        path = request.url.path
        if path == "/tiktok" and request.method == "POST":
            _metrics["tiktok_post_total"] += 1
        elif path == "/douyin" and request.method == "POST":
            _metrics["douyin_post_total"] += 1
        elif path == "/tiktok/download" and request.method == "GET":
            _metrics["tiktok_download_total"] += 1
        response = await call_next(request)
        if response.status_code >= 400:
            if path == "/tiktok":
                _metrics["tiktok_post_errors"] += 1
            elif path == "/douyin":
                _metrics["douyin_post_errors"] += 1
            elif path == "/tiktok/download":
                _metrics["tiktok_download_errors"] += 1
        return response


def build_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting TikTokDownloader Gateway (env=%s)", ENVIRONMENT)
        try:
            await start_engine()
        except Exception:
            logger.exception("Engine bootstrap failed")
            raise
        yield
        logger.info("Shutting down gateway...")
        await close_engine()
        await session_store.close_session_store()
        await extraction_cache.close_extraction_cache()
        logger.info("Shutdown complete")

    app = FastAPI(
        title="TikTokDownloader Gateway",
        version=__version__,
        description=(
            "REST API gateway for TikTok/Douyin download using TikTokDownloader engine. "
            "Compatible with tiktok-api-dl endpoints."
        ),
        lifespan=lifespan,
        docs_url=None if IS_PROD else "/docs",
        redoc_url=None if IS_PROD else "/redoc",
        openapi_url=None if IS_PROD else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "X-API-Key"],
    )
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(MetricsMiddleware)

    register_routes(app)

    @app.get("/")
    async def handle_root() -> Dict[str, Any]:
        return {
            "service": "tiktokdownloader-gateway",
            "status": "ok",
            "version": __version__,
            "transport": "fastapi + TikTokDownloader",
            "endpoints": [
                "/",
                "/health",
                "/metrics",
                "/tiktok",
                "/douyin",
                "/tiktok/download",
            ],
        }

    @app.get("/health")
    async def handle_health() -> Dict[str, Any]:
        sessions = await session_store.active_session_count()
        redis_ok = await session_store.redis_ping()
        return {
            "status": "ok" if redis_ok else "degraded",
            "time": datetime.datetime.utcnow().isoformat() + "Z",
            "version": DEFAULT_VERSION,
            "environment": ENVIRONMENT,
            "active_sessions": sessions,
            "session_backend": "redis" if session_store.is_redis_backend() else "memory",
            "extract_cache_backend": (
                "redis" if extraction_cache.is_extract_cache_redis() else "memory"
            ),
            "extract_cache_ttl_seconds": extraction_cache.extract_cache_ttl(),
            "redis_ok": redis_ok,
            "ffmpeg": FFMPEG_PATH,
            "engine": "tiktokdownloader",
        }

    @app.get("/metrics")
    async def handle_metrics() -> Dict[str, Any]:
        return {**_metrics, "environment": ENVIRONMENT}

    return app
