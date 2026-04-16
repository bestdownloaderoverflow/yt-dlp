#!/usr/bin/env python3
"""
TCP JSON-RPC server wrapper for worker_daemon dispatch logic.

Protocol over TCP (line-delimited JSON):
- Input: one JSON object per line
- Output: one JSON object per line
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import sys
import threading
import traceback
import importlib.util
from typing import Any, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - optional in local dev
    psutil = None
import yt_dlp

# Keep import path stable inside container
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_DAEMON_PATH = os.path.join(PROJECT_ROOT, "extractor", "worker_daemon.py")
_SPEC = importlib.util.spec_from_file_location("worker_daemon_local", _DAEMON_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Failed to load worker daemon module from {_DAEMON_PATH}")
daemon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(daemon)


logger = logging.getLogger("extractor_worker_server")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def _write_message(wfile, payload: dict[str, Any]) -> None:
    wfile.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
    wfile.flush()


def _memory_snapshot() -> tuple[int, int]:
    if psutil is not None:
        mem = psutil.virtual_memory()
        return int(mem.total), int(mem.available)
    try:
        mem_total = 1024 ** 3
        mem_available = mem_total
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        mem_total = int(parts[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        mem_available = int(parts[1]) * 1024
        return mem_total, mem_available
    except OSError:
        fallback = 1024 ** 3
        return fallback, fallback


def _read_parallel_capacity() -> int:
    raw = os.getenv("YTDLP_WORKER_CONCURRENCY", "auto").strip().lower()
    if raw not in {"", "auto"}:
        try:
            configured = int(raw)
        except ValueError:
            configured = 0
        if configured > 0:
            return configured

    cpu_count = (psutil.cpu_count(logical=True) if psutil is not None else None) or os.cpu_count() or 1
    mem_total, mem_available = _memory_snapshot()
    mem_gib = max(1, int(mem_total / (1024 ** 3)))
    capacity = min(max(1, cpu_count * 2), max(1, mem_gib * 2), 32)
    if mem_available / max(1, mem_total) < 0.20:
        capacity = max(1, capacity // 2)
    return max(1, capacity)


class ConcurrencyLimiter:
    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self._sem = threading.Semaphore(self.capacity)
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self) -> None:
        self._sem.acquire()
        with self._lock:
            self._active += 1

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1
        self._sem.release()

    def active(self) -> int:
        with self._lock:
            return self._active


LIMITER = ConcurrencyLimiter(_read_parallel_capacity())


class RPCHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            raw = self.rfile.readline()
            if not raw:
                return

            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            req_id: Optional[Any] = None
            try:
                request = json.loads(line)
                req_id = request.get("id")
                method = request.get("method")
                params = request.get("params") or {}

                if not method or not isinstance(method, str):
                    _write_message(self.wfile, daemon._error_response(req_id, "Missing required field: method", code="bad_request", status=400))
                    continue
                if not isinstance(params, dict):
                    _write_message(self.wfile, daemon._error_response(req_id, "Field params must be an object", code="bad_request", status=400))
                    continue

                LIMITER.acquire()
                try:
                    result = daemon._dispatch(method, params)
                finally:
                    LIMITER.release()
                _write_message(self.wfile, {"id": req_id, "ok": True, "result": result})
            except yt_dlp.utils.DownloadError as exc:
                _write_message(self.wfile, daemon._error_response(req_id, str(exc), code="download_error", status=400))
            except ValueError as exc:
                _write_message(self.wfile, daemon._error_response(req_id, str(exc), code="bad_request", status=400))
            except RuntimeError as exc:
                payload = None
                try:
                    payload = json.loads(str(exc))
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    _write_message(
                        self.wfile,
                        daemon._error_response(
                            req_id,
                            payload.get("message", "runtime error"),
                            code=payload.get("code", "runtime_error"),
                            status=int(payload.get("status", 500)),
                        ),
                    )
                else:
                    _write_message(self.wfile, daemon._error_response(req_id, str(exc), code="runtime_error", status=500))
            except Exception as exc:
                logger.error("Unhandled server error: %s\n%s", exc, traceback.format_exc())
                _write_message(self.wfile, daemon._error_response(req_id, str(exc), code="internal_error", status=500))


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    host = os.getenv("EXTRACTOR_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("EXTRACTOR_BIND_PORT", "9487"))
    with ThreadingTCPServer((host, port), RPCHandler) as server:
        logger.info(
            "extractor worker server listening on %s:%d with auto concurrency=%d",
            host,
            port,
            LIMITER.capacity,
        )
        server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
