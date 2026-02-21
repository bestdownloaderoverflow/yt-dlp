"""Health check and monitoring endpoints."""

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request as FastAPIRequest

from process_manager import process_manager
from security import is_localhost
from ytdl_manager import ydl_manager

router = APIRouter()
logger = logging.getLogger("ytdl_stream")


@router.get("/health")
async def health_check():
    """
    Health check endpoint untuk monitoring.

    Returns:
    - System health status
    - Active processes count
    - YoutubeDL manager stats
    - Memory/CPU usage
    """
    import psutil

    process = psutil.Process()
    memory_info = process.memory_info()

    return {
        "status": "healthy",
        "version": "3.0.0",
        "uptime_seconds": int(time.time() - process.create_time()),
        "system": {
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory_mb": memory_info.rss / 1024 / 1024,
            "threads": process.num_threads(),
        },
        "managers": {
            "ytdl": ydl_manager.stats,
            "processes": process_manager.stats,
        },
    }


@router.get("/stats")
async def get_stats(
    request: FastAPIRequest = None,
):
    """
    Internal statistics endpoint.

    Security: Hanya accessible dari localhost.
    """
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        raise HTTPException(status_code=403, detail="Forbidden")

    import psutil

    process = psutil.Process()

    return {
        "ytdl_manager": ydl_manager.stats,
        "process_manager": process_manager.stats,
        "system": {
            "pid": process.pid,
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory": {
                "rss_mb": process.memory_info().rss / 1024 / 1024,
                "vms_mb": process.memory_info().vms / 1024 / 1024,
            },
            "threads": process.num_threads(),
            "open_files": len(process.open_files()),
            "connections": len(process.connections()),
        },
    }


@router.post("/admin/cleanup")
async def admin_cleanup(
    request: FastAPIRequest = None,
):
    """
    Force cleanup endpoint untuk maintenance.

    Security: Hanya accessible dari localhost.
    """
    client_host = request.client.host if request.client else ""
    if not is_localhost(client_host):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Cleanup YoutubeDL instances
    ydl_manager.force_cleanup()

    # Cleanup processes (will terminate stuck processes)
    process_manager._cleanup_old_processes()
    process_manager._cleanup_dead_processes()

    return {
        "status": "cleanup completed",
        "remaining_processes": len(process_manager._processes),
    }
