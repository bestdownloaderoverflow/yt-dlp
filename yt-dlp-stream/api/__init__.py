"""API routes for yt-dlp-stream."""

from fastapi import APIRouter

from api import info, stream_ffmpeg, stream_chunked, stream_direct, internal, health, fetch, download

# Main router
router = APIRouter()

# Include all sub-routers
router.include_router(info.router)
router.include_router(stream_ffmpeg.router)
router.include_router(stream_chunked.router)
router.include_router(stream_direct.router)
router.include_router(internal.router)
router.include_router(health.router)
router.include_router(fetch.router)
router.include_router(download.router)

__all__ = ["router"]
