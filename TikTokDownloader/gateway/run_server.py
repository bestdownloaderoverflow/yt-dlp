"""ASGI entrypoint for gateway mode.

Usage:
  uvicorn gateway.run_server:app --host 0.0.0.0 --port 7790
  python -m gateway.run_server
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root is on sys.path when run as script/module
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gateway.app import build_app

app = build_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("SERVER_PORT", "7790"))
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    uvicorn.run(
        "gateway.run_server:app",
        host=host,
        port=port,
        log_level=log_level,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
