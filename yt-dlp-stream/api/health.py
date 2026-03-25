"""Health check and monitoring endpoints."""

import copy
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request as FastAPIRequest

from process_manager import process_manager
from security import is_localhost
from ytdl_manager import ydl_manager

router = APIRouter()
logger = logging.getLogger("ytdl_stream")
_HEALTH_LOCK = threading.Lock()
_HEALTH_STARTED = False
_HEALTH_SNAPSHOT = {
    "status": "starting",
    "version": "3.0.0",
    "uptime_seconds": 0,
    "system": {"cpu_percent": 0.0, "memory_mb": 0.0, "threads": 0},
    "managers": {
        "ytdl": {"request_count": 0, "active_instances": 0, "max_instances": 0, "instances": {}},
        "processes": {
            "total_processes": 0,
            "processes_by_type": {},
            "oldest_process_age": None,
            "processes": [],
        },
    },
}


def _cpu_percent_non_blocking(process) -> float:
    """
    Return CPU usage without sleeping in the request path.

    psutil.Process.cpu_percent(interval=0.1) blocks for the sampling window,
    which makes health checks more likely to pile up under load.
    """
    try:
        return process.cpu_percent(interval=None)
    except Exception:
        return 0.0


def _build_health_snapshot() -> dict:
    import psutil

    process = psutil.Process()
    memory_info = process.memory_info()

    return {
        "status": "healthy",
        "version": "3.0.0",
        "uptime_seconds": int(time.time() - process.create_time()),
        "system": {
            "cpu_percent": _cpu_percent_non_blocking(process),
            "memory_mb": memory_info.rss / 1024 / 1024,
            "threads": process.num_threads(),
        },
        "managers": {
            "ytdl": ydl_manager.stats,
            "processes": process_manager.stats,
        },
    }


def _health_snapshot_updater() -> None:
    while True:
        try:
            snapshot = _build_health_snapshot()
            with _HEALTH_LOCK:
                _HEALTH_SNAPSHOT.clear()
                _HEALTH_SNAPSHOT.update(snapshot)
        except Exception:
            logger.exception("Failed to refresh health snapshot")
        time.sleep(2)


def _ensure_health_snapshot_worker() -> None:
    global _HEALTH_STARTED

    with _HEALTH_LOCK:
        if _HEALTH_STARTED:
            return
        thread = threading.Thread(
            target=_health_snapshot_updater,
            daemon=True,
            name="HealthSnapshot",
        )
        thread.start()
        _HEALTH_STARTED = True


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
    _ensure_health_snapshot_worker()
    with _HEALTH_LOCK:
        return copy.deepcopy(_HEALTH_SNAPSHOT)


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
            "cpu_percent": _cpu_percent_non_blocking(process),
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
