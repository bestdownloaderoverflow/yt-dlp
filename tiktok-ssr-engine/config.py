import os
import random
from pathlib import Path
from typing import List, Optional

from proxy_state import get_in_flight_many, get_proxy_exit_ips_many, is_proxy_usable

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
    int(i) for i in os.getenv("INDONESIA_PROXIES", "1,13,14").split(",") if i.strip().isdigit()
]

# Provider layout of the wireproxy pool, matching the index ranges written by
# yt-dlp-wireproxy/scripts/generate-wireproxy-configs.py. Both bounds inclusive;
# WARP_FIRST_INDEX runs to PROXY_COUNT.
SURFSHARK_FIRST_INDEX = 1
SURFSHARK_LAST_INDEX = 12
MULLVAD_FIRST_INDEX = 13
MULLVAD_LAST_INDEX = 17
WARP_FIRST_INDEX = 18

def _proxies(first: int, last: int) -> List[str]:
    """SOCKS5 URLs for wireproxy nodes in the inclusive index range [first, last]."""
    return [
        f"socks5h://{PROXY_HOST_PREFIX}-{i:02d}:{PROXY_PORT}"
        for i in range(first, last + 1)
    ]


def get_proxy_pool() -> List[str]:
    """
    Returns list of SOCKS5 proxies from PROXY_LIST or auto-generated wireproxy-01..wireproxy-NN.
    Interleaves regions (ID -> SG -> VN -> ID -> SG -> VN...) so every 3 consecutive attempts
    are guaranteed to touch different geographic regions!
    """
    if RAW_PROXY_LIST:
        return [p.strip() for p in RAW_PROXY_LIST.split(",") if p.strip()]
    if PROXY_COUNT >= WARP_FIRST_INDEX:
        # Group 1: 18..PROXY_COUNT (Cloudflare WARP - 4 nodes)
        # Group 2: 13..17 (Mullvad IPv4 SEA - 5 nodes)
        # Group 3: 01..12 (Surfshark IPv4 SEA - 12 nodes)
        warp_list = _proxies(WARP_FIRST_INDEX, PROXY_COUNT)
        mullvad_list = _proxies(MULLVAD_FIRST_INDEX, MULLVAD_LAST_INDEX)
        surfshark_list = _proxies(SURFSHARK_FIRST_INDEX, SURFSHARK_LAST_INDEX)

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
        return _proxies(1, PROXY_COUNT)
    if DEFAULT_PROXY:
        return [DEFAULT_PROXY]
    return []

def get_warp_proxies() -> List[str]:
    if PROXY_COUNT >= WARP_FIRST_INDEX:
        return _proxies(WARP_FIRST_INDEX, PROXY_COUNT)
    return []


def get_geo_proxies() -> List[str]:
    """Returns Surfshark 01..12 + Mullvad 13..17 (SEA IPv4 exits) for geo-locked content."""
    if PROXY_COUNT >= MULLVAD_LAST_INDEX:
        surfshark = _proxies(SURFSHARK_FIRST_INDEX, SURFSHARK_LAST_INDEX)
        mullvad = _proxies(MULLVAD_FIRST_INDEX, MULLVAD_LAST_INDEX)
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


def _pick_from(candidates: List[str], exclude: Optional[set[str]] = None,
               exclude_exit_ips: Optional[set[str]] = None) -> Optional[str]:
    """Pick a least-loaded candidate while weighting each public exit IP once."""
    excluded = exclude or set()
    excluded_ips = exclude_exit_ips or set()
    usable = [p for p in candidates if p and p not in excluded and is_proxy_usable(p)]
    if not usable:
        return None

    loads = get_in_flight_many(usable)
    exit_ips = get_proxy_exit_ips_many(usable)

    # Multiple WARP configs can resolve to one public IP. Treat that as one exit
    # group so three containers sharing an IP do not receive triple traffic.
    groups: dict[str, list[str]] = {}
    for proxy in usable:
        exit_ip = exit_ips.get(proxy, "")
        if exit_ip and exit_ip in excluded_ips:
            continue
        group_key = exit_ip or f"unknown:{proxy}"
        groups.setdefault(group_key, []).append(proxy)
    if not groups:
        return None

    group_loads = {
        group_key: sum(loads.get(proxy, 0) for proxy in proxies)
        for group_key, proxies in groups.items()
    }
    best_group_load = min(group_loads.values())
    best_groups = [key for key, load in group_loads.items() if load == best_group_load]
    selected_group = random.choice(best_groups)
    proxies = groups[selected_group]
    best_proxy_load = min(loads.get(proxy, 0) for proxy in proxies)
    best_proxies = [proxy for proxy in proxies if loads.get(proxy, 0) == best_proxy_load]
    return random.choice(best_proxies)


def get_next_proxy(prefer_geo: bool = False, indo_only: bool = False,
                   warp_only: bool = False, exclude: Optional[set[str]] = None,
                   exclude_exit_ips: Optional[set[str]] = None) -> Optional[str]:
    if warp_only:
        # Strict on purpose: the first attempt asks for Cloudflare or nothing, so
        # the caller decides the fallback instead of silently landing on a geo
        # node it was trying to save.
        return _pick_from(get_warp_proxies(), exclude, exclude_exit_ips)
    if indo_only:
        # Strict like warp_only: callers explicitly decide the fallback lane.
        return _pick_from(get_indo_proxies(), exclude, exclude_exit_ips)
    if prefer_geo:
        p = _pick_from(get_geo_proxies(), exclude, exclude_exit_ips)
        if p:
            return p
    p = _pick_from(get_proxy_pool(), exclude, exclude_exit_ips)
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
