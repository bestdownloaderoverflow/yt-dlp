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
import traceback
import importlib.util
from typing import Any, Optional

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

                result = daemon._dispatch(method, params)
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
        logger.info("extractor worker server listening on %s:%d", host, port)
        server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
