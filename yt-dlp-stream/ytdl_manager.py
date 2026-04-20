"""
Singleton YoutubeDL Manager untuk session integrity.
Memastikan CookieJar direuse antar request seperti yt_dlp CLI.

Key improvements:
- Multiple instances per config (tidak reset CookieJar)
- LRU cache untuk cleanup automatic
- Better error handling & logging
"""
import threading
import logging
import time
import os
import random
import shutil
from typing import Optional, Dict, Any, List
from collections import OrderedDict
try:
    import psutil
except ImportError:  # pragma: no cover - optional in local dev
    psutil = None
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

logger = logging.getLogger("uvicorn.error")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    if value < 1:
        return None
    return value


def _total_memory_bytes() -> int:
    if psutil is not None:
        return int(psutil.virtual_memory().total)
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except OSError:
        pass
    return 1024 ** 3


def _default_tiktok_device_id() -> str:
    """Return a stable process-level random TikTok-like device id (19 digits)."""
    return str(random.randint(7250000000000000000, 7325099899999994577))


def _default_js_runtime_config() -> Dict[str, Dict[str, str]]:
    deno_path = shutil.which("deno")
    if deno_path:
        return {"deno": {"path": deno_path}}
    return {"deno": {}}


def _env_js_runtimes() -> Dict[str, Dict[str, str]]:
    """
    Parse YTDLP_JS_RUNTIMES into yt-dlp API format:
    runtime[:path],runtime2[:path2]
    """
    raw = os.getenv("YTDLP_JS_RUNTIMES")
    if not raw or not raw.strip():
        return _default_js_runtime_config()

    runtimes: Dict[str, Dict[str, str]] = {}
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue

        name, path = (token.split(":", 1) + [""])[:2]
        name = name.strip().lower()
        path = path.strip()
        if not name:
            continue

        cfg: Dict[str, str] = {}
        if path:
            cfg["path"] = path
        elif name == "deno":
            deno_path = shutil.which("deno")
            if deno_path:
                cfg["path"] = deno_path

        runtimes[name] = cfg

    return runtimes or _default_js_runtime_config()


def _check_impersonate_available(target: str) -> bool:
    """One-time check whether an impersonate target is supported by installed libraries."""
    try:
        t = ImpersonateTarget.from_str(target)
        ydl = yt_dlp.YoutubeDL({"quiet": True, "impersonate": t})
        ydl.close()
        return True
    except Exception:
        return False


class YoutubeDLManager:
    """
    Thread-safe singleton manager untuk YoutubeDL instance.

    yt_dlp menggunakan global CookieJar per instance. Jika instance di-destroy,
    cookieJar hilang. Singleton pattern memastikan session persistent.
    """
    _instance = None
    _instance_lock = threading.Lock()
    # Cache impersonate availability checks: target_str -> bool
    _impersonate_cache: dict = {}
    _impersonate_cache_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_manager()
        return cls._instance

    def _init_manager(self):
        """Initialize manager state."""
        # Multiple instances per config untuk preserve CookieJar
        self._ydl_instances: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._pool_slots: Dict[str, List[str]] = {}
        self._pool_rr: Dict[str, int] = {}
        self._pool_conds: Dict[str, threading.Condition] = {}
        self._global_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._request_count = 0
        self._parallel_per_key = self._detect_parallel_per_key()
        self._max_instances = max(10, self._parallel_per_key * 4)  # LRU cache size
        self._cleanup_interval = 3600  # Cleanup every hour
        self._last_cleanup = time.time()
        self._tiktok_device_id = os.getenv("TIKTOK_DEVICE_ID", _default_tiktok_device_id()).strip()
        self._tiktok_app_name = os.getenv("TIKTOK_APP_NAME", "trill").strip() or "trill"
        self._tiktok_app_version = os.getenv("TIKTOK_APP_VERSION", "34.1.2").strip() or "34.1.2"
        self._tiktok_manifest_app_version = (
            os.getenv("TIKTOK_MANIFEST_APP_VERSION", "2023401020").strip() or "2023401020"
        )
        self._tiktok_aid = os.getenv("TIKTOK_AID", "1180").strip() or "1180"
        # Optional full override (single value or comma-separated values).
        self._tiktok_app_info_override = os.getenv("TIKTOK_APP_INFO", "").strip()
        self._js_runtimes = _env_js_runtimes()
        logger.info(
            "YoutubeDL manager parallelism enabled: per_key=%d total_max_instances=%d",
            self._parallel_per_key,
            self._max_instances,
        )
        logger.info(
            "yt-dlp JS runtimes configured: %s",
            ", ".join(
                f"{name}:{cfg.get('path')}" if cfg.get("path") else name
                for name, cfg in self._js_runtimes.items()
            ),
        )

    def _detect_parallel_per_key(self) -> int:
        configured = _env_int("YTDLP_PARALLEL_PER_KEY")
        if configured is not None:
            return configured

        cpu_count = (psutil.cpu_count(logical=True) if psutil is not None else None) or os.cpu_count() or 1
        mem_total = _total_memory_bytes()
        mem_gib = max(1, int(mem_total / (1024 ** 3)))
        parallel = min(max(1, cpu_count * 2), max(1, mem_gib * 2), 32)
        return max(1, parallel)

    def _build_tiktok_app_info_list(self) -> List[str]:
        if self._tiktok_app_info_override:
            return [v.strip() for v in self._tiktok_app_info_override.split(",") if v.strip()]
        return [
            (
                f"{self._tiktok_device_id}/"
                f"{self._tiktok_app_name}/"
                f"{self._tiktok_app_version}/"
                f"{self._tiktok_manifest_app_version}/"
                f"{self._tiktok_aid}"
            )
        ]

    @staticmethod
    def _build_opts_key(proxy: Optional[str], impersonate: Optional[str], force_ipv6: bool) -> str:
        return f"{proxy or 'none'}:{impersonate or 'none'}:{'v6' if force_ipv6 else 'default'}"

    def _create_ydl(self, proxy: Optional[str] = None,
                    impersonate: Optional[str] = None,
                    force_ipv6: bool = False) -> yt_dlp.YoutubeDL:
        """Create new YoutubeDL instance dengan given options."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "js_runtimes": {name: dict(cfg) for name, cfg in self._js_runtimes.items()},
            # Keep single-video behavior for watch URLs containing list/start_radio.
            # Can be overridden with YTDLP_NO_PLAYLIST=false.
            "noplaylist": _env_bool("YTDLP_NO_PLAYLIST", True),
            # Fail fast when VPN/upstream is slow so the Go gateway can
            # retry on a different worker within its own IPC timeout
            # window instead of blocking for 90s on a stuck request.
            "socket_timeout": 25,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            # Force TikTok extractor to try mobile API first.
            # If mobile API path fails, yt-dlp TikTok extractor already
            # falls back to web extraction automatically.
            "extractor_args": {
                "tiktok": {
                    "app_info": self._build_tiktok_app_info_list(),
                    "device_id": [self._tiktok_device_id],
                }
            },
        }
        if proxy:
            opts["proxy"] = proxy
        if force_ipv6:
            # Force outbound socket bind to IPv6 family only.
            opts["source_address"] = "::"
        if impersonate:
            target_str = impersonate if isinstance(impersonate, str) else str(impersonate)
            with YoutubeDLManager._impersonate_cache_lock:
                if target_str not in YoutubeDLManager._impersonate_cache:
                    YoutubeDLManager._impersonate_cache[target_str] = _check_impersonate_available(target_str)
                    if YoutubeDLManager._impersonate_cache[target_str]:
                        logger.info(f"Impersonate target '{target_str}' is available")
                    else:
                        logger.warning(
                            f"Impersonate target '{target_str}' not available. "
                            f"Falling back to default. Install 'curl_cffi' for impersonate support."
                        )
                available = YoutubeDLManager._impersonate_cache[target_str]
            if available:
                impersonate_target = (
                    impersonate
                    if isinstance(impersonate, ImpersonateTarget)
                    else ImpersonateTarget.from_str(target_str)
                )
                opts["impersonate"] = impersonate_target

        return yt_dlp.YoutubeDL(opts)

    @staticmethod
    def _build_outbound_headers_locked(ydl: yt_dlp.YoutubeDL, info_dict: Dict) -> Dict[str, str]:
        """
        Build outbound HTTP headers with cookiejar parity.

        yt-dlp's _calc_headers intentionally strips Cookie from returned headers.
        For direct HTTP proxying/streaming, we re-add scoped cookies from cookiejar
        for the target URL so requests more closely match downloader behaviour.
        """
        headers = dict(ydl._calc_headers(info_dict, load_cookies=True))
        url = info_dict.get("url")
        if url:
            cookie_header = ydl.cookiejar.get_cookie_header(url)
            if cookie_header:
                headers["Cookie"] = cookie_header
        # Match HttpFD behaviour by disabling compression for predictable byte ranges
        headers.setdefault("Accept-Encoding", "identity")
        return headers

    def _cleanup_old_instances(self):
        """LRU cleanup: hapus instance terlama jika melebihi limit."""
        while len(self._ydl_instances) > self._max_instances:
            oldest_key = next(iter(self._ydl_instances))
            removed = self._ydl_instances.get(oldest_key)
            if removed and removed.get("busy"):
                self._ydl_instances.move_to_end(oldest_key)
                break
            removed = self._ydl_instances.pop(oldest_key, None)
            if not removed:
                continue
            opts_key = removed.get("opts_key")
            if opts_key and opts_key in self._pool_slots:
                self._pool_slots[opts_key] = [slot for slot in self._pool_slots[opts_key] if slot != oldest_key]
                if not self._pool_slots[opts_key]:
                    self._pool_slots.pop(opts_key, None)
                    self._pool_rr.pop(opts_key, None)
                    self._pool_conds.pop(opts_key, None)
            logger.info(f"Cleaned up YoutubeDL instance: {oldest_key}")

    def _periodic_cleanup_locked(self):
        """Periodic cleanup untuk idle instances. Caller must hold _global_lock."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        # Hapus instances yang idle > 1 jam
        to_remove = []
        for key, data in self._ydl_instances.items():
            if data.get("busy"):
                continue
            if now - data.get('last_used', now) > 3600:
                to_remove.append(key)

        for key in to_remove:
            removed = self._ydl_instances.pop(key, None)
            if removed:
                opts_key = removed.get("opts_key")
                if opts_key and opts_key in self._pool_slots:
                    self._pool_slots[opts_key] = [slot for slot in self._pool_slots[opts_key] if slot != key]
                    if not self._pool_slots[opts_key]:
                        self._pool_slots.pop(opts_key, None)
                        self._pool_rr.pop(opts_key, None)
                        self._pool_conds.pop(opts_key, None)
            logger.info(f"Removed idle instance: {key}")

        self._last_cleanup = now

    def _periodic_cleanup(self):
        """Periodic cleanup untuk idle instances."""
        with self._global_lock:
            self._periodic_cleanup_locked()

    def _create_slot_locked(self, opts_key: str, proxy: Optional[str],
                            impersonate: Optional[str], force_ipv6: bool) -> str:
        slots = self._pool_slots.setdefault(opts_key, [])
        slot_key = f"{opts_key}#{len(slots) + 1}"
        self._ydl_instances[slot_key] = {
            "instance": self._create_ydl(proxy, impersonate, force_ipv6),
            "created": time.time(),
            "last_used": time.time(),
            "opts_key": opts_key,
            "busy": False,
        }
        slots.append(slot_key)
        self._pool_conds.setdefault(opts_key, threading.Condition(self._global_lock))
        self._pool_rr.setdefault(opts_key, 0)
        logger.info("Created new YoutubeDL instance: %s", slot_key)
        self._cleanup_old_instances()
        return slot_key

    def _acquire_ydl_slot(self, proxy: Optional[str] = None,
                          impersonate: Optional[str] = None,
                          force_ipv6: bool = False) -> tuple[str, yt_dlp.YoutubeDL]:
        opts_key = self._build_opts_key(proxy, impersonate, force_ipv6)

        with self._global_lock:
            self._periodic_cleanup_locked()
            slots = self._pool_slots.setdefault(opts_key, [])
            if not slots:
                self._create_slot_locked(opts_key, proxy, impersonate, force_ipv6)
                slots = self._pool_slots[opts_key]

            cond = self._pool_conds.setdefault(opts_key, threading.Condition(self._global_lock))
            while True:
                slots = self._pool_slots.get(opts_key, [])
                slot_count = len(slots)
                if slot_count == 0:
                    self._create_slot_locked(opts_key, proxy, impersonate, force_ipv6)
                    slots = self._pool_slots[opts_key]
                    slot_count = len(slots)

                start = self._pool_rr.get(opts_key, 0) % max(1, slot_count)
                for offset in range(slot_count):
                    slot_key = slots[(start + offset) % slot_count]
                    slot = self._ydl_instances.get(slot_key)
                    if not slot or slot.get("busy"):
                        continue
                    slot["busy"] = True
                    slot["last_used"] = time.time()
                    self._ydl_instances.move_to_end(slot_key)
                    self._pool_rr[opts_key] = (start + offset + 1) % slot_count
                    return slot_key, slot["instance"]

                if slot_count < self._parallel_per_key and len(self._ydl_instances) < self._max_instances:
                    slot_key = self._create_slot_locked(opts_key, proxy, impersonate, force_ipv6)
                    slot = self._ydl_instances[slot_key]
                    slot["busy"] = True
                    slot["last_used"] = time.time()
                    self._ydl_instances.move_to_end(slot_key)
                    self._pool_rr[opts_key] = len(self._pool_slots[opts_key]) % max(1, len(self._pool_slots[opts_key]))
                    return slot_key, slot["instance"]

                cond.wait(timeout=0.5)

    def _release_ydl_slot(self, slot_key: str) -> None:
        with self._global_lock:
            slot = self._ydl_instances.get(slot_key)
            if not slot:
                return
            slot["busy"] = False
            slot["last_used"] = time.time()
            opts_key = slot.get("opts_key")
            cond = self._pool_conds.get(opts_key)
            if cond is not None:
                cond.notify()

    def extract_info(self, url: str, proxy: Optional[str] = None,
                     impersonate: Optional[str] = None,
                     force_ipv6: bool = False) -> Dict:
        """
        Thread-safe extract_info dengan session integrity.

        Locking diperlukan karena yt_dlp tidak thread-safe.
        Setiap config punya lock sendiri untuk better concurrency.
        """
        slot_key, ydl = self._acquire_ydl_slot(proxy, impersonate, force_ipv6)
        try:
            with self._count_lock:
                self._request_count += 1
            try:
                info = ydl.extract_info(url, download=False)
                # Attach cookie-aware headers from THIS instance to every format so
                # that calc_headers() callers don't need to re-acquire a (possibly
                # different) slot.  Without this, round-robin pooling causes a
                # different CookieJar to be used for the Cookie header, breaking
                # TikTok CDN requests that validate tt_chain_token per-session.
                if isinstance(info, dict):
                    self._attach_headers_locked(ydl, info)
                return info
            except Exception as e:
                logger.error(f"extract_info failed for {url}: {e}")
                raise
        finally:
            self._release_ydl_slot(slot_key)

    @staticmethod
    def _attach_headers_locked(ydl: yt_dlp.YoutubeDL, info: Dict) -> None:
        """Attach pre-computed cookie headers to info dict and all its formats."""
        formats = info.get("formats")
        if formats:
            for fmt in formats:
                if fmt.get("url"):
                    fmt["__ydl_headers"] = YoutubeDLManager._build_outbound_headers_locked(ydl, fmt)
        if info.get("url"):
            info["__ydl_headers"] = YoutubeDLManager._build_outbound_headers_locked(ydl, info)
            info["http_headers"] = info["__ydl_headers"]

    def resolve_formats(self, info: Dict, format_str: str,
                        proxy: Optional[str] = None,
                        impersonate: Optional[str] = None) -> List[Dict]:
        """Resolve format string menggunakan yt_dlp's format selector."""
        slot_key, ydl = self._acquire_ydl_slot(proxy, impersonate, False)
        try:
            try:
                selector = ydl.build_format_selector(format_str)
                selected = list(selector(info))

                if not selected:
                    raise ValueError(f"No format matching '{format_str}' found")

                top = selected[0]
                if "requested_formats" in top:
                    resolved = [dict(fmt) for fmt in top["requested_formats"]]
                else:
                    resolved = [dict(top)]

                # Ensure each selected format has outbound-ready headers (incl. cookies)
                for fmt in resolved:
                    if fmt.get("url"):
                        fmt["http_headers"] = self._build_outbound_headers_locked(ydl, fmt)

                return resolved
            except Exception as e:
                logger.error(f"resolve_formats failed: {e}")
                raise
        finally:
            self._release_ydl_slot(slot_key)

    def calc_headers(self, info_dict: Dict, load_cookies: bool = True,
                     proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Calculate headers termasuk cookies dari CookieJar.

        Ini yang memastikan session cookies dikirim dengan request.
        Jika info_dict sudah punya __ydl_headers (di-attach saat extract_info),
        langsung return itu supaya cookie dari instance yang sama dipakai.
        """
        if load_cookies and "__ydl_headers" in info_dict:
            return dict(info_dict["__ydl_headers"])

        slot_key, ydl = self._acquire_ydl_slot(proxy, impersonate, False)
        try:
            try:
                if load_cookies:
                    return self._build_outbound_headers_locked(ydl, info_dict)
                return dict(ydl._calc_headers(info_dict, load_cookies=False))
            except Exception as e:
                logger.error(f"calc_headers failed: {e}")
                raise
        finally:
            self._release_ydl_slot(slot_key)

    def get_cookiejar(self, proxy: Optional[str] = None,
                      impersonate: Optional[str] = None):
        """Access cookiejar untuk debugging/monitoring."""
        slot_key, ydl = self._acquire_ydl_slot(proxy, impersonate, False)
        try:
            return ydl.cookiejar
        finally:
            self._release_ydl_slot(slot_key)

    @property
    def stats(self) -> Dict:
        """Return manager statistics."""
        with self._global_lock:
            return {
                "request_count": self._request_count,
                "active_instances": len(self._ydl_instances),
                "max_instances": self._max_instances,
                "parallel_per_key": self._parallel_per_key,
                "instances": {
                    key: {
                        "created": data.get('created'),
                        "last_used": data.get('last_used'),
                        "age_seconds": time.time() - data.get('created', time.time()),
                        "busy": bool(data.get("busy")),
                        "opts_key": data.get("opts_key"),
                    }
                    for key, data in self._ydl_instances.items()
                },
            }

    def force_cleanup(self):
        """Force cleanup all instances (untuk testing/maintenance)."""
        with self._global_lock:
            self._ydl_instances.clear()
            self._pool_slots.clear()
            self._pool_rr.clear()
            self._pool_conds.clear()
            logger.info("Force cleanup: all instances removed")


# Global singleton instance
ydl_manager = YoutubeDLManager()
