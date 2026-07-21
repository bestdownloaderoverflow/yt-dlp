"""Bootstrap TikTokDownloader engine for headless gateway use."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from src.application.main_terminal import TikTok
from src.config import Parameter, Settings
from src.custom import PROJECT_ROOT, TEXT_REPLACEMENT
from src.manager import Database, DownloadRecorder
from src.module import Cookie, MigrateFolder
from src.record import BaseLogger
from src.tools import ColorfulConsole

logger = logging.getLogger("gateway.engine")

_DOUYIN_RE = re.compile(
    r"(douyin\.com|iesdouyin\.com|amemv\.com|webcast\.amemv)",
    re.I,
)
_TIKTOK_RE = re.compile(
    r"(tiktok\.com|vt\.tiktok|vm\.tiktok|tiktokv\.com)",
    re.I,
)


class EngineError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status = status
        self.code = code


class EngineAdapter:
    """Singleton-style holder for Parameter + TikTok handler."""

    def __init__(self) -> None:
        self.console = ColorfulConsole(debug=False)
        self.settings = Settings(PROJECT_ROOT, self.console)
        self.cookie = Cookie(self.settings, self.console)
        self.database: Optional[Database] = None
        self.recorder: Optional[DownloadRecorder] = None
        self.parameter: Optional[Parameter] = None
        self.handler: Optional[TikTok] = None
        self._ready = False

    async def start(self) -> None:
        if self._ready:
            return
        self.database = Database()
        await self.database.__aenter__()
        self.recorder = DownloadRecorder(self.database, True, self.console)
        self._apply_env_cookies()
        self.parameter = Parameter(
            self.settings,
            self.cookie,
            logger=BaseLogger,
            console=self.console,
            **self.settings.read(),
            recorder=self.recorder,
        )
        # Gateway never writes files to disk for API extract path
        self.parameter.download = False
        MigrateFolder(self.parameter).compatible()
        self.parameter.set_headers_cookie()
        self.parameter.CLEANER.set_rule(TEXT_REPLACEMENT, True)
        self.handler = TikTok(self.parameter, self.database, server_mode=True)
        self._ready = True
        logger.info(
            "Engine ready (douyin_cookie=%s tiktok_cookie=%s)",
            bool(self.parameter.cookie_str or self.parameter.cookie_dict),
            bool(self.parameter.cookie_str_tiktok or self.parameter.cookie_dict_tiktok),
        )

    def _apply_env_cookies(self) -> None:
        data = self.settings.read()
        changed = False
        dy = os.environ.get("DOUYIN_COOKIE", "").strip()
        tt = os.environ.get("TIKTOK_COOKIE", "").strip()
        if dy and self.cookie.validate_cookie_minimal(dy):
            data["cookie"] = self.cookie.extract(dy, write=False, key="cookie", platform="抖音")
            changed = True
            logger.info("Loaded DOUYIN_COOKIE from environment")
        if tt and self.cookie.validate_cookie_minimal(tt):
            data["cookie_tiktok"] = self.cookie.extract(
                tt, write=False, key="cookie_tiktok", platform="TikTok"
            )
            changed = True
            logger.info("Loaded TIKTOK_COOKIE from environment")
        proxy = os.environ.get("PROXY", "").strip()
        proxy_tt = os.environ.get("PROXY_TIKTOK", "").strip() or proxy
        if proxy:
            data["proxy"] = proxy
            changed = True
        if proxy_tt:
            data["proxy_tiktok"] = proxy_tt
            changed = True
        if changed:
            self.settings.update(data)

    async def close(self) -> None:
        if self.parameter:
            try:
                await self.parameter.close_client()
            except Exception as exc:
                logger.warning("close_client error: %s", exc)
        if self.database:
            try:
                await self.database.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("database close error: %s", exc)
        self._ready = False
        self.handler = None
        self.parameter = None

    @staticmethod
    def detect_platform(url: str) -> str:
        if _DOUYIN_RE.search(url):
            return "douyin"
        if _TIKTOK_RE.search(url):
            return "tiktok"
        raise EngineError(f"Cannot detect platform from URL: {url}", 400, "bad_request")

    async def extract_detail(
        self,
        url: str,
        *,
        platform: Optional[str] = None,
        proxy: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        if not self._ready or not self.handler:
            raise EngineError("Engine not ready", 503, "not_ready")

        platform = platform or self.detect_platform(url)
        tiktok = platform == "tiktok"

        detail_ids = await self._resolve_detail_ids(url, tiktok=tiktok, proxy=proxy)
        if not detail_ids:
            raise EngineError("Could not extract post ID from URL", 400, "not_found")

        detail_id = detail_ids[0]
        root, params, logger_factory = self.handler.record.run(self.parameter)
        async with logger_factory(root, console=self.console, **params) as record:
            data = await self.handler._handle_detail(
                [detail_id],
                tiktok,
                record,
                api=True,
                source=False,
                cookie=cookie or None,
                proxy=proxy or None,
            )
        if not data:
            raise EngineError(
                "Failed to fetch post data (cookie/encrypt/params may be invalid)",
                502,
                "extract_failed",
            )
        item = data[0] if isinstance(data, list) else data
        if not isinstance(item, dict):
            raise EngineError("Unexpected extract result type", 502, "extract_failed")
        item["_platform"] = platform
        item["_source_url"] = url
        return platform, item

    async def _resolve_detail_ids(
        self,
        url: str,
        *,
        tiktok: bool,
        proxy: Optional[str],
    ) -> list:
        links = self.handler.links_tiktok if tiktok else self.handler.links
        try:
            result = await links.run(url, "detail", proxy)
        except Exception as exc:
            logger.warning("link resolve failed: %s", exc)
            result = None
        if isinstance(result, list):
            return [str(i) for i in result if i]
        if isinstance(result, str) and result.isdigit():
            return [result]
        # Fallback: bare numeric id in URL
        m = re.search(r"/video/(\d+)", url) or re.search(r"/photo/(\d+)", url)
        if m:
            return [m.group(1)]
        m = re.search(r"/note/(\d+)", url) or re.search(r"/slides/(\d+)", url)
        if m:
            return [m.group(1)]
        m = re.search(r"\b(\d{15,25})\b", url)
        if m:
            return [m.group(1)]
        return []


_engine: Optional[EngineAdapter] = None


def get_engine() -> EngineAdapter:
    global _engine
    if _engine is None:
        _engine = EngineAdapter()
    return _engine


async def start_engine() -> EngineAdapter:
    eng = get_engine()
    await eng.start()
    return eng


async def close_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.close()
        _engine = None
