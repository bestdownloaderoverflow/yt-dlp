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
from typing import Optional, Dict, Any, List
from collections import OrderedDict
import yt_dlp

logger = logging.getLogger("uvicorn.error")


class YoutubeDLManager:
    """
    Thread-safe singleton manager untuk YoutubeDL instance.

    yt_dlp menggunakan global CookieJar per instance. Jika instance di-destroy,
    cookieJar hilang. Singleton pattern memastikan session persistent.
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
        # Multiple instances per config untuk preserve CookieJar
        self._ydl_instances: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._request_count = 0
        self._max_instances = 10  # LRU cache size
        self._cleanup_interval = 3600  # Cleanup every hour
        self._last_cleanup = time.time()

    def _create_ydl(self, proxy: Optional[str] = None,
                    impersonate: Optional[str] = None) -> yt_dlp.YoutubeDL:
        """Create new YoutubeDL instance dengan given options."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        if proxy:
            opts["proxy"] = proxy
        if impersonate:
            opts["impersonate"] = impersonate

        return yt_dlp.YoutubeDL(opts)

    def _cleanup_old_instances(self):
        """LRU cleanup: hapus instance terlama jika melebihi limit."""
        while len(self._ydl_instances) > self._max_instances:
            oldest_key = next(iter(self._ydl_instances))
            removed = self._ydl_instances.pop(oldest_key)
            if oldest_key in self._locks:
                del self._locks[oldest_key]
            logger.info(f"Cleaned up YoutubeDL instance: {oldest_key}")

    def _periodic_cleanup_locked(self):
        """Periodic cleanup untuk idle instances. Caller must hold _global_lock."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        # Hapus instances yang idle > 1 jam
        to_remove = []
        for key, data in self._ydl_instances.items():
            if now - data.get('last_used', now) > 3600:
                to_remove.append(key)

        for key in to_remove:
            self._ydl_instances.pop(key, None)
            self._locks.pop(key, None)
            logger.info(f"Removed idle instance: {key}")

        self._last_cleanup = now

    def _periodic_cleanup(self):
        """Periodic cleanup untuk idle instances."""
        with self._global_lock:
            self._periodic_cleanup_locked()

    def _get_ydl(self, proxy: Optional[str] = None,
                 impersonate: Optional[str] = None) -> yt_dlp.YoutubeDL:
        """
        Get existing atau create new YoutubeDL instance per config.

        CRITICAL: Setiap config punya instance sendiri, jadi CookieJar
        tidak di-reset. Ini memastikan session integrity preserved.
        """
        opts_key = f"{proxy or 'none'}:{impersonate or 'none'}"

        with self._global_lock:
            # Periodic cleanup
            self._periodic_cleanup_locked()

            if opts_key not in self._ydl_instances:
                # Create new instance dan lock
                if opts_key not in self._locks:
                    self._locks[opts_key] = threading.Lock()

                self._ydl_instances[opts_key] = {
                    'instance': self._create_ydl(proxy, impersonate),
                    'created': time.time(),
                    'last_used': time.time(),
                }
                logger.info(f"Created new YoutubeDL instance: {opts_key}")

                # LRU cleanup
                self._cleanup_old_instances()
            else:
                # Move to end (LRU)
                self._ydl_instances.move_to_end(opts_key)
                self._ydl_instances[opts_key]['last_used'] = time.time()

            return self._ydl_instances[opts_key]['instance']

    def extract_info(self, url: str, proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Thread-safe extract_info dengan session integrity.

        Locking diperlukan karena yt_dlp tidak thread-safe.
        Setiap config punya lock sendiri untuk better concurrency.
        """
        opts_key = f"{proxy or 'none'}:{impersonate or 'none'}"

        # Get ydl instance (creates if needed)
        ydl = self._get_ydl(proxy, impersonate)

        # Lock per-instance, bukan global
        with self._locks[opts_key]:
            self._request_count += 1
            try:
                return ydl.extract_info(url, download=False)
            except Exception as e:
                logger.error(f"extract_info failed for {url}: {e}")
                raise

    def resolve_formats(self, info: Dict, format_str: str,
                        proxy: Optional[str] = None,
                        impersonate: Optional[str] = None) -> List[Dict]:
        """Resolve format string menggunakan yt_dlp's format selector."""
        opts_key = f"{proxy or 'none'}:{impersonate or 'none'}"
        ydl = self._get_ydl(proxy, impersonate)

        with self._locks[opts_key]:
            try:
                selector = ydl.build_format_selector(format_str)
                selected = list(selector(info))

                if not selected:
                    raise ValueError(f"No format matching '{format_str}' found")

                top = selected[0]
                if "requested_formats" in top:
                    return list(top["requested_formats"])
                return [top]
            except Exception as e:
                logger.error(f"resolve_formats failed: {e}")
                raise

    def calc_headers(self, info_dict: Dict, load_cookies: bool = True,
                     proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Calculate headers termasuk cookies dari CookieJar.

        Ini yang memastikan session cookies dikirim dengan request.
        """
        opts_key = f"{proxy or 'none'}:{impersonate or 'none'}"
        ydl = self._get_ydl(proxy, impersonate)

        with self._locks[opts_key]:
            try:
                return ydl._calc_headers(info_dict, load_cookies=load_cookies)
            except Exception as e:
                logger.error(f"calc_headers failed: {e}")
                raise

    def get_cookiejar(self, proxy: Optional[str] = None,
                      impersonate: Optional[str] = None):
        """Access cookiejar untuk debugging/monitoring."""
        opts_key = f"{proxy or 'none'}:{impersonate or 'none'}"
        ydl = self._get_ydl(proxy, impersonate)

        with self._locks[opts_key]:
            return ydl.cookiejar

    @property
    def stats(self) -> Dict:
        """Return manager statistics."""
        with self._global_lock:
            return {
                "request_count": self._request_count,
                "active_instances": len(self._ydl_instances),
                "max_instances": self._max_instances,
                "instances": {
                    key: {
                        "created": data.get('created'),
                        "last_used": data.get('last_used'),
                        "age_seconds": time.time() - data.get('created', time.time())
                    }
                    for key, data in self._ydl_instances.items()
                },
            }

    def force_cleanup(self):
        """Force cleanup all instances (untuk testing/maintenance)."""
        with self._global_lock:
            self._ydl_instances.clear()
            self._locks.clear()
            logger.info("Force cleanup: all instances removed")


# Global singleton instance
ydl_manager = YoutubeDLManager()
