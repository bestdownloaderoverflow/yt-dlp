"""
Singleton YoutubeDL Manager untuk session integrity.
Memastikan CookieJar direuse antar request seperti yt_dlp CLI.

Key improvements:
- Instance pool per config key (N instances, up to N concurrent extractions)
- LRU cache untuk cleanup automatic
- Better error handling & logging
"""
import os
import threading
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from collections import OrderedDict
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

logger = logging.getLogger("uvicorn.error")

# How many yt-dlp instances to keep per proxy:impersonate config key.
# This controls max concurrency for the same proxy — higher values allow
# more parallel extractions but use more memory and cookies diverge.
POOL_SIZE_PER_KEY = 3

# Directory for persisting cookie files across container restarts.
# Each yt-dlp instance gets its own cookie file keyed by config+slot index.
COOKIE_DIR = Path(os.getenv("COOKIE_DIR", "/tmp/ytdlp_cookies"))


class _InstanceSlot:
    """A single yt-dlp instance with its own lock."""
    __slots__ = ("ydl", "lock", "created", "last_used", "busy")

    def __init__(self, ydl: yt_dlp.YoutubeDL):
        self.ydl = ydl
        self.lock = threading.Lock()
        self.created = time.time()
        self.last_used = time.time()
        self.busy = False


class YoutubeDLManager:
    """
    Thread-safe singleton manager untuk YoutubeDL instance.

    yt_dlp menggunakan global CookieJar per instance. Jika instance di-destroy,
    cookieJar hilang. Singleton pattern memastikan session persistent.

    Each config key (proxy:impersonate) maps to a *pool* of up to
    POOL_SIZE_PER_KEY instances so concurrent requests on the same
    proxy don't serialize behind a single lock.
    """
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_manager()
        return cls._instance

    def _init_manager(self):
        """Initialize manager state."""
        # key -> list[_InstanceSlot]
        self._pools: OrderedDict[str, List[_InstanceSlot]] = OrderedDict()
        self._global_lock = threading.Lock()
        self._count_lock = threading.Lock()  # Dedicated lock for request counter
        self._request_count = 0
        self._max_keys = 30  # LRU cache size (number of distinct config keys)
        self._cleanup_interval = 3600  # Cleanup every hour
        self._last_cleanup = time.time()

    @staticmethod
    def _cookie_path(opts_key: str, slot_index: int) -> str:
        """Return the file path for persisting cookies for a given slot.

        Includes os.getpid() so each Granian worker process writes to a
        separate file, preventing cookie corruption from concurrent writes.
        """
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        safe_key = opts_key.replace(":", "_").replace("/", "_")
        pid = os.getpid()
        return str(COOKIE_DIR / f"cookies_{safe_key}_{pid}_{slot_index}.txt")

    def _create_ydl(self, proxy: Optional[str] = None,
                    impersonate: Optional[str] = None,
                    cookie_file: Optional[str] = None) -> yt_dlp.YoutubeDL:
        """Create new YoutubeDL instance dengan given options."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        if cookie_file:
            opts["cookiefile"] = cookie_file
        if proxy:
            opts["proxy"] = proxy
        if impersonate:
            try:
                impersonate_target = (
                    impersonate
                    if isinstance(impersonate, ImpersonateTarget)
                    else ImpersonateTarget.from_str(impersonate)
                )
                # Check if impersonate is available before setting
                test_opts = {"quiet": True, "impersonate": impersonate_target}
                test_ydl = yt_dlp.YoutubeDL(test_opts)
                test_ydl.close()
                opts["impersonate"] = impersonate_target
                logger.info(f"Using impersonate target: {impersonate}")
            except Exception as e:
                logger.warning(
                    f"Impersonate target '{impersonate}' not available: {e}. "
                    f"Falling back to default. Install 'curl_cffi' for impersonate support."
                )
                # Continue without impersonate

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

    def _cleanup_old_keys(self):
        """LRU cleanup: remove oldest config keys when over limit."""
        while len(self._pools) > self._max_keys:
            oldest_key = next(iter(self._pools))
            self._pools.pop(oldest_key)
            logger.info(f"Cleaned up YoutubeDL pool: {oldest_key}")

    def _periodic_cleanup_locked(self):
        """Periodic cleanup untuk idle pools. Caller must hold _global_lock."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        # Remove pools where all slots idle > 1 hour
        to_remove = []
        for key, slots in self._pools.items():
            if all(now - s.last_used > 3600 and not s.busy for s in slots):
                to_remove.append(key)

        for key in to_remove:
            self._pools.pop(key, None)
            logger.info(f"Removed idle pool: {key}")

        self._last_cleanup = now

    def _acquire_slot(self, proxy: Optional[str] = None,
                      impersonate: Optional[str] = None) -> _InstanceSlot:
        """
        Acquire a yt-dlp instance slot from the pool.

        Strategy:
        1. Try to find an unlocked (idle) slot in existing pool.
        2. If all slots busy but pool < POOL_SIZE_PER_KEY, create a new slot.
        3. If pool is full, wait on the least-recently-used slot's lock.

        Returns the slot with its lock already acquired.
        """
        opts_key = f"{proxy or 'none'}:{impersonate or 'none'}"

        with self._global_lock:
            self._periodic_cleanup_locked()

            if opts_key not in self._pools:
                self._pools[opts_key] = []
                logger.info(f"Created new YoutubeDL pool: {opts_key}")
                self._cleanup_old_keys()

            # Move to end (LRU)
            self._pools.move_to_end(opts_key)
            pool = self._pools[opts_key]

            # 1. Try to grab an idle slot without blocking
            for slot in pool:
                if slot.lock.acquire(blocking=False):
                    slot.busy = True
                    slot.last_used = time.time()
                    return slot

            # 2. Pool not full — create a new instance
            if len(pool) < POOL_SIZE_PER_KEY:
                slot_index = len(pool)
                cookie_file = self._cookie_path(opts_key, slot_index)
                ydl = self._create_ydl(proxy, impersonate, cookie_file=cookie_file)
                slot = _InstanceSlot(ydl)
                pool.append(slot)
                slot.lock.acquire()
                slot.busy = True
                logger.info(
                    f"Pool {opts_key}: added instance #{len(pool)} "
                    f"(pool size: {len(pool)}/{POOL_SIZE_PER_KEY})"
                )
                return slot

            # 3. Pool full — pick the LRU slot and wait for its lock
            lru_slot = min(pool, key=lambda s: s.last_used)

        # Block outside _global_lock so other keys can proceed
        lru_slot.lock.acquire()
        lru_slot.busy = True
        lru_slot.last_used = time.time()
        return lru_slot

    def _release_slot(self, slot: _InstanceSlot):
        """Release a slot back to the pool."""
        slot.busy = False
        slot.lock.release()

    def extract_info(self, url: str, proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Thread-safe extract_info dengan session integrity.

        Acquires a pooled instance so up to POOL_SIZE_PER_KEY concurrent
        extractions can run for the same proxy:impersonate config.
        """
        slot = self._acquire_slot(proxy, impersonate)
        try:
            with self._count_lock:
                self._request_count += 1
            info = slot.ydl.extract_info(url, download=False)
            if isinstance(info, dict) and info.get("url"):
                info["http_headers"] = self._build_outbound_headers_locked(slot.ydl, info)
            return info
        except Exception as e:
            logger.error(f"extract_info failed for {url}: {e}")
            raise
        finally:
            self._release_slot(slot)

    def resolve_formats(self, info: Dict, format_str: str,
                        proxy: Optional[str] = None,
                        impersonate: Optional[str] = None) -> List[Dict]:
        """Resolve format string menggunakan yt_dlp's format selector."""
        slot = self._acquire_slot(proxy, impersonate)
        try:
            selector = slot.ydl.build_format_selector(format_str)
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
                    fmt["http_headers"] = self._build_outbound_headers_locked(slot.ydl, fmt)

            return resolved
        except Exception as e:
            logger.error(f"resolve_formats failed: {e}")
            raise
        finally:
            self._release_slot(slot)

    def calc_headers(self, info_dict: Dict, load_cookies: bool = True,
                     proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Calculate headers termasuk cookies dari CookieJar.

        Ini yang memastikan session cookies dikirim dengan request.
        """
        slot = self._acquire_slot(proxy, impersonate)
        try:
            if load_cookies:
                return self._build_outbound_headers_locked(slot.ydl, info_dict)
            return dict(slot.ydl._calc_headers(info_dict, load_cookies=False))
        except Exception as e:
            logger.error(f"calc_headers failed: {e}")
            raise
        finally:
            self._release_slot(slot)

    def get_cookiejar(self, proxy: Optional[str] = None,
                      impersonate: Optional[str] = None):
        """Access cookiejar untuk debugging/monitoring."""
        slot = self._acquire_slot(proxy, impersonate)
        try:
            return slot.ydl.cookiejar
        finally:
            self._release_slot(slot)

    @property
    def stats(self) -> Dict:
        """Return manager statistics."""
        with self._global_lock:
            pool_stats = {}
            for key, slots in self._pools.items():
                pool_stats[key] = {
                    "pool_size": len(slots),
                    "busy": sum(1 for s in slots if s.busy),
                    "slots": [
                        {
                            "created": s.created,
                            "last_used": s.last_used,
                            "busy": s.busy,
                            "age_seconds": time.time() - s.created,
                        }
                        for s in slots
                    ],
                }
            return {
                "request_count": self._request_count,
                "active_pools": len(self._pools),
                "max_keys": self._max_keys,
                "pool_size_per_key": POOL_SIZE_PER_KEY,
                "pools": pool_stats,
            }

    def force_cleanup(self):
        """Force cleanup all instances (untuk testing/maintenance)."""
        with self._global_lock:
            self._pools.clear()
            logger.info("Force cleanup: all pools removed")


# Global singleton instance
ydl_manager = YoutubeDLManager()
