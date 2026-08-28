import json
import os
import random
from pathlib import Path
from typing import Dict, List, Optional

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

# Pool manifest written by yt-dlp-wireproxy/scripts/generate-wireproxy-configs.py.
# The .conf files record a node's country only in a comment, so without this the
# engine cannot tell Singapore from Jakarta and has to treat "two distinct exit
# IPv4s failed" as geo evidence -- which proves nothing when 8 of 17 geo nodes
# sit in one country. Optional: with no manifest the country map is empty, the
# country preference becomes a no-op, and behaviour is exactly as before.
POOL_MANIFEST_PATHS = [
    os.getenv("PROXY_POOL_FILE", ""),
    "/app/runtime-configs/pool.json",
    str(Path(__file__).resolve().parent.parent / "yt-dlp-wireproxy" / "runtime-configs" / "pool.json"),
]


def _load_pool_manifest() -> Dict[int, dict]:
    for path in POOL_MANIFEST_PATHS:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = {
                entry["index"]: entry
                for entry in (data.get("proxies") or [])
                if isinstance(entry.get("index"), int)
            }
            if entries:
                return entries
        except Exception as exc:
            print(f"[Config] pool manifest {path} unreadable: {exc}", flush=True)
    return {}


POOL_MANIFEST = _load_pool_manifest()


def _proxy_url(index: int) -> str:
    return f"socks5h://{PROXY_HOST_PREFIX}-{index:02d}:{PROXY_PORT}"


# Exits whose country is unknown (WARP anycast, or no manifest) are absent here
# rather than mapped to a placeholder: an unknown country must never be used as
# evidence that two attempts landed in the same place.
PROXY_COUNTRIES: Dict[str, str] = {
    _proxy_url(index): str(entry["country"]).upper()
    for index, entry in POOL_MANIFEST.items()
    if entry.get("country")
}

def derive_indonesia_indexes(manifest: Dict[int, dict], env_value: str = "") -> List[int]:
    """An explicit INDONESIA_PROXIES wins; otherwise take every ID node.

    Deriving it means re-running the generator cannot leave this pointing at
    nodes that have since moved out of Indonesia, which is the failure mode of
    a hand-copied list.
    """
    if env_value.strip():
        return [int(i) for i in env_value.split(",") if i.strip().isdigit()]
    return sorted(
        index for index, entry in manifest.items()
        if str(entry.get("country") or "").upper() == "ID"
    )


_INDONESIA_ENV = os.getenv("INDONESIA_PROXIES", "").strip()
INDONESIA_PROXY_INDEXES = derive_indonesia_indexes(POOL_MANIFEST, _INDONESIA_ENV)
if not INDONESIA_PROXY_INDEXES and PROXY_COUNT > 0:
    print("[Config] no Indonesia exits: INDONESIA_PROXIES is unset and no pool "
          f"manifest was found in {[p for p in POOL_MANIFEST_PATHS if p]}. "
          "The Indonesia recovery lane is disabled.", flush=True)


def get_proxy_country(proxy_url: str) -> str:
    """Country code for an exit, or "" when unknown (WARP anycast, no manifest)."""
    return PROXY_COUNTRIES.get(proxy_url, "")

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
    """Every proxy the service may route through: PROXY_LIST, or wireproxy-01..NN.

    This is the complete set by construction -- the warp/geo/indo helpers all
    carve out subranges of 1..PROXY_COUNT -- so callers that need "all proxies"
    can use this directly rather than unioning the subset helpers.

    Order carries no meaning. _pick_from() selects by in-flight load with a
    random tie-break, so no caller ever reads the sequence.
    """
    if RAW_PROXY_LIST:
        return [p.strip() for p in RAW_PROXY_LIST.split(",") if p.strip()]
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
               exclude_exit_ips: Optional[set[str]] = None,
               avoid_countries: Optional[set[str]] = None) -> Optional[str]:
    """Pick a least-loaded candidate while weighting each public exit IP once."""
    excluded = exclude or set()
    excluded_ips = exclude_exit_ips or set()
    usable = [p for p in candidates if p and p not in excluded and is_proxy_usable(p)]
    if not usable:
        return None

    # Country is a preference, never a filter. Retrying in a country that has
    # already failed is weak evidence -- 8 of the 17 geo nodes are in Singapore,
    # so "two distinct exit IPv4s" can easily mean two Singapore exits. But
    # enforcing it would empty the pool on the countries that hold most of it,
    # so fall back to the full set when spreading is impossible. Exits with an
    # unknown country stay eligible: absence of evidence is not a match.
    if avoid_countries:
        spread = [p for p in usable if get_proxy_country(p) not in avoid_countries]
        if spread:
            usable = spread

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
                   exclude_exit_ips: Optional[set[str]] = None,
                   avoid_countries: Optional[set[str]] = None) -> Optional[str]:
    if warp_only:
        # Strict on purpose: the first attempt asks for Cloudflare or nothing, so
        # the caller decides the fallback instead of silently landing on a geo
        # node it was trying to save.
        return _pick_from(get_warp_proxies(), exclude, exclude_exit_ips)
    if indo_only:
        # Strict like warp_only: callers explicitly decide the fallback lane.
        # No country preference here -- this lane exists to reach one country.
        return _pick_from(get_indo_proxies(), exclude, exclude_exit_ips)
    if prefer_geo:
        p = _pick_from(get_geo_proxies(), exclude, exclude_exit_ips, avoid_countries)
        if p:
            return p
    p = _pick_from(get_proxy_pool(), exclude, exclude_exit_ips, avoid_countries)
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
