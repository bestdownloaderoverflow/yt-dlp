import os
import random
from pathlib import Path
from typing import List, Optional

from proxy_state import get_in_flight, is_proxy_usable

PORT = int(os.getenv("PORT", "9111"))
HOST = os.getenv("HOST", "0.0.0.0")
TIKTOK_API_KEY = os.getenv("TIKTOK_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")
DEFAULT_PROXY = os.getenv("DEFAULT_PROXY", "")
DEFAULT_IMPERSONATE = os.getenv("DEFAULT_IMPERSONATE", "chrome120")
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # 5 minutes extraction cache
VERBOSE_LOGS = os.getenv("VERBOSE_LOGS", "0").lower() in ("1", "true", "yes")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))

# Proxy Pool Configuration
PROXY_COUNT = int(os.getenv("PROXY_COUNT", "0"))
PROXY_HOST_PREFIX = os.getenv("PROXY_HOST_PREFIX", "wireproxy")
PROXY_PORT = int(os.getenv("PROXY_PORT", "1080"))
RAW_PROXY_LIST = os.getenv("PROXY_LIST", "")
INDONESIA_PROXY_INDEXES = [
    int(i) for i in os.getenv("INDONESIA_PROXIES", "1,2,6,7").split(",") if i.strip().isdigit()
]

def get_proxy_pool() -> List[str]:
    """
    Returns list of SOCKS5 proxies from PROXY_LIST or auto-generated wireproxy-01..wireproxy-18.
    Interleaves regions (ID -> SG -> VN -> ID -> SG -> VN...) so every 3 consecutive attempts
    are guaranteed to touch different geographic regions!
    """
    if RAW_PROXY_LIST:
        return [p.strip() for p in RAW_PROXY_LIST.split(",") if p.strip()]
    if PROXY_COUNT >= 12:
        # Group 1: 12..PROXY_COUNT (Cloudflare WARP IPv6 Anycast - 39 nodes)
        # Group 2: 07..11 (Mullvad IPv4 ID/SG - 5 nodes)
        # Group 3: 01..06 (Surfshark IPv4 ID/SG - 6 nodes)
        warp_list = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(12, PROXY_COUNT + 1)]
        mullvad_list = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(7, 12)]
        surfshark_list = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(1, 7)]

        # Interleave Geo nodes (Surfshark + Mullvad alternating)
        geo_list = []
        for i in range(max(len(surfshark_list), len(mullvad_list))):
            if i < len(surfshark_list):
                geo_list.append(surfshark_list[i])
            if i < len(mullvad_list):
                geo_list.append(mullvad_list[i])

        # Distribute Geo nodes evenly across WARP nodes (Uniform Zigzag: ~3 WARP -> 1 Geo -> ~3 WARP -> 1 Geo)
        final_pool = []
        w_idx = 0
        g_idx = 0
        total_warp = len(warp_list)
        total_geo = len(geo_list)
        step = total_warp / total_geo if total_geo > 0 else total_warp

        while w_idx < total_warp or g_idx < total_geo:
            target_warp = int((g_idx + 1) * step) if g_idx < total_geo else total_warp
            while w_idx < target_warp and w_idx < total_warp:
                final_pool.append(warp_list[w_idx])
                w_idx += 1
            if g_idx < total_geo:
                final_pool.append(geo_list[g_idx])
                g_idx += 1

        return final_pool
    if PROXY_COUNT > 0:
        return [
            f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}"
            for i in range(1, PROXY_COUNT + 1)
        ]
    if DEFAULT_PROXY:
        return [DEFAULT_PROXY]
    return []

def get_warp_proxies() -> List[str]:
    if PROXY_COUNT >= 12:
        return [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(12, PROXY_COUNT + 1)]
    return []


def get_geo_proxies() -> List[str]:
    """Returns Indonesia & Singapore proxies (Surfshark 01..06 + Mullvad 07..11) for geo-locked content."""
    if PROXY_COUNT >= 11:
        surfshark = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(1, 7)]
        mullvad = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(7, 12)]
        res = []
        for i in range(max(len(surfshark), len(mullvad))):
            if i < len(surfshark):
                res.append(surfshark[i])
            if i < len(mullvad):
                res.append(mullvad[i])
        return res
    return []


def get_indo_proxies() -> List[str]:
    """Returns Indonesia-only proxies for region-restricted content."""
    return [
        f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}"
        for i in INDONESIA_PROXY_INDEXES
        if 1 <= i <= PROXY_COUNT
    ]


def _pick_from(candidates: List[str]) -> Optional[str]:
    """Pick the best candidate: skip dead/cooldown proxies, prefer least-loaded."""
    usable = [p for p in candidates if p and is_proxy_usable(p)]
    if not usable:
        return None
    usable.sort(key=lambda p: (get_in_flight(p), random.random()))
    return usable[0]


def get_next_proxy(prefer_geo: bool = False, indo_only: bool = False) -> Optional[str]:
    if indo_only:
        p = _pick_from(get_indo_proxies())
        if p:
            return p
    if prefer_geo:
        p = _pick_from(get_geo_proxies())
        if p:
            return p
    p = _pick_from(get_proxy_pool())
    if p:
        return p
    return DEFAULT_PROXY or None

# Cookie file path search
COOKIE_PATHS = [
    os.getenv("COOKIE_FILE", ""),
    str(Path(__file__).resolve().parent.parent / "yt-dlp-wireproxy" / "cookie" / "cookie.txt"),
    str(Path(__file__).resolve().parent / "cookie.txt"),
    "/app/cookie/cookie.txt",
]

def get_cookie_file() -> Optional[str]:
    for p in COOKIE_PATHS:
        if p and os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None

def load_cookie_string() -> str:
    cookie_file = get_cookie_file()
    if not cookie_file:
        return ""
    try:
        cookies = []
        with open(cookie_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name = parts[5].strip()
                    value = parts[6].strip()
                    cookies.append(f"{name}={value}")
        return "; ".join(cookies)
    except Exception:
        return ""
