import os
import random
from pathlib import Path
from typing import List, Optional

PORT = int(os.getenv("PORT", "9111"))
HOST = os.getenv("HOST", "0.0.0.0")
TIKTOK_API_KEY = os.getenv("TIKTOK_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")
DEFAULT_PROXY = os.getenv("DEFAULT_PROXY", "")
DEFAULT_IMPERSONATE = os.getenv("DEFAULT_IMPERSONATE", "chrome120")
CACHE_TTL = int(os.getenv("CACHE_TTL", "120"))  # 2 minutes extraction cache
VERBOSE_LOGS = os.getenv("VERBOSE_LOGS", "0").lower() in ("1", "true", "yes")

# Proxy Pool Configuration
PROXY_COUNT = int(os.getenv("PROXY_COUNT", "0"))
PROXY_HOST_PREFIX = os.getenv("PROXY_HOST_PREFIX", "wireproxy")
PROXY_PORT = int(os.getenv("PROXY_PORT", "1080"))
RAW_PROXY_LIST = os.getenv("PROXY_LIST", "")

def get_proxy_pool() -> List[str]:
    """
    Returns list of SOCKS5 proxies from PROXY_LIST or auto-generated wireproxy-01..wireproxy-18.
    Interleaves regions (ID -> SG -> VN -> ID -> SG -> VN...) so every 3 consecutive attempts
    are guaranteed to touch different geographic regions!
    """
    if RAW_PROXY_LIST:
        return [p.strip() for p in RAW_PROXY_LIST.split(",") if p.strip()]
    if PROXY_COUNT >= 12:
        # Group 1: 12..PROXY_COUNT (Cloudflare WARP IPv6 Anycast)
        # Group 2: 07..11 (Mullvad Dual-Stack ID/SG)
        # Group 3: 01..06 (Surfshark IPv4 ID/SG)
        warp_list = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(12, PROXY_COUNT + 1)]
        mullvad_list = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(7, 12)]
        surfshark_list = [f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}" for i in range(1, 7)]
        
        interleaved = []
        max_len = max(len(warp_list), len(mullvad_list), len(surfshark_list))
        for i in range(max_len):
            if i < len(warp_list):
                interleaved.append(warp_list[i])
            interleaved.append(mullvad_list[i % len(mullvad_list)])
            interleaved.append(surfshark_list[i % len(surfshark_list)])
            if i + 1 < len(warp_list):
                interleaved.append(warp_list[i + 1])
        
        # Preserve all unique proxies in balanced order
        seen = set()
        final_pool = []
        for p in interleaved:
            if p not in seen and len(final_pool) < PROXY_COUNT:
                seen.add(p)
                final_pool.append(p)
        # Add any remaining warp nodes
        for p in warp_list:
            if p not in seen and len(final_pool) < PROXY_COUNT:
                seen.add(p)
                final_pool.append(p)
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


_proxy_index = random.randint(0, 1000)
_geo_index = random.randint(0, 1000)


def get_next_proxy(prefer_geo: bool = False) -> Optional[str]:
    global _proxy_index, _geo_index
    if prefer_geo:
        geo_pool = get_geo_proxies()
        if geo_pool:
            p = geo_pool[_geo_index % len(geo_pool)]
            _geo_index = (_geo_index + 1) % len(geo_pool)
            return p

    pool = get_proxy_pool()
    if not pool:
        return DEFAULT_PROXY or None
    p = pool[_proxy_index % len(pool)]
    _proxy_index = (_proxy_index + 1) % len(pool)
    return p

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
