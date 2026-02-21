"""
Singleton YoutubeDL Manager untuk session integrity.
Memastikan CookieJar direuse antar request seperti yt_dlp CLI.
"""
import threading
from typing import Optional, Dict, Any, List
import yt_dlp


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
        self._ydl: Optional[yt_dlp.YoutubeDL] = None
        self._ydl_lock = threading.Lock()
        self._current_opts: Dict[str, Any] = {}
        self._request_count = 0

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

    def _get_ydl(self, proxy: Optional[str] = None,
                 impersonate: Optional[str] = None) -> yt_dlp.YoutubeDL:
        """
        Get existing atau create new YoutubeDL instance.

        Jika options berubah, recreate instance (trade-off: cookieJar reset).
        Ini acceptable karena yt_dlp juga tidak support multiple configs simultaneously.
        """
        opts_key = f"{proxy}:{impersonate}"

        if self._ydl is None or self._current_opts.get("key") != opts_key:
            self._ydl = self._create_ydl(proxy, impersonate)
            self._current_opts = {"key": opts_key, "proxy": proxy, "impersonate": impersonate}

        return self._ydl

    def extract_info(self, url: str, proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Thread-safe extract_info dengan session integrity.

        Locking diperlukan karena yt_dlp tidak thread-safe.
        Requests akan serialized tapi tetap lebih baik dari instance baru tiap request.
        """
        with self._ydl_lock:
            ydl = self._get_ydl(proxy, impersonate)
            self._request_count += 1
            return ydl.extract_info(url, download=False)

    def resolve_formats(self, info: Dict, format_str: str,
                        proxy: Optional[str] = None,
                        impersonate: Optional[str] = None) -> List[Dict]:
        """Resolve format string menggunakan yt_dlp's format selector."""
        with self._ydl_lock:
            ydl = self._get_ydl(proxy, impersonate)
            selector = ydl.build_format_selector(format_str)
            selected = list(selector(info))

            if not selected:
                raise ValueError(f"No format matching '{format_str}' found")

            top = selected[0]
            if "requested_formats" in top:
                return list(top["requested_formats"])
            return [top]

    def calc_headers(self, info_dict: Dict, load_cookies: bool = True,
                     proxy: Optional[str] = None,
                     impersonate: Optional[str] = None) -> Dict:
        """
        Calculate headers termasuk cookies dari CookieJar.

        Ini yang memastikan session cookies dikirim dengan request.
        """
        with self._ydl_lock:
            ydl = self._get_ydl(proxy, impersonate)
            return ydl._calc_headers(info_dict, load_cookies=load_cookies)

    def get_cookiejar(self, proxy: Optional[str] = None,
                      impersonate: Optional[str] = None):
        """Access cookiejar untuk debugging/monitoring."""
        with self._ydl_lock:
            ydl = self._get_ydl(proxy, impersonate)
            return ydl.cookiejar

    @property
    def stats(self) -> Dict:
        """Return manager statistics."""
        return {
            "request_count": self._request_count,
            "current_proxy": self._current_opts.get("proxy"),
            "current_impersonate": self._current_opts.get("impersonate"),
        }


# Global singleton instance
ydl_manager = YoutubeDLManager()
