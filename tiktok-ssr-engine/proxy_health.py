"""Background SOCKS5 health probers for the wireproxy pool.

Two independent loops run as asyncio tasks:

  liveness (fast)  - probes every proxy through its SOCKS5 tunnel and marks
                     broken tunnels dead, clearing the mark once they answer.
  block (slow)     - asks TikTok itself whether it still serves this exit IP,
                     and marks the proxy blocked when it does not. A tunnel can
                     be perfectly healthy while TikTok refuses its IP, which the
                     liveness probe cannot see.

The block loop hits TikTok, so it holds a short Redis lease to make sure only
one Granian worker runs it instead of every worker probing the same pool.
"""

import asyncio
import os
import time

from config import get_geo_proxies, get_indo_proxies, get_proxy_pool
from proxy_state import (
    clear_proxy_blocked,
    mark_proxy_blocked,
    mark_proxy_dead,
    mark_proxy_usable,
)

PROBE_INTERVAL_SECONDS = float(os.getenv("PROBE_INTERVAL", "45"))
PROBE_TIMEOUT_SECONDS = float(os.getenv("PROBE_TIMEOUT", "6"))
PROBE_CONCURRENCY = int(os.getenv("PROBE_CONCURRENCY", "12"))
PROBE_DEAD_AFTER = int(os.getenv("PROBE_DEAD_AFTER", "2"))

BLOCK_PROBE_INTERVAL_SECONDS = float(os.getenv("BLOCK_PROBE_INTERVAL", "300"))
BLOCK_PROBE_TIMEOUT_SECONDS = float(os.getenv("BLOCK_PROBE_TIMEOUT", "20"))
BLOCK_PROBE_CONCURRENCY = int(os.getenv("BLOCK_PROBE_CONCURRENCY", "6"))
# The embed endpoint, not a profile or video page. Measured across the pool:
# /@handle and /@handle/video/<id> both return a 1462-byte stub on every node
# regardless of health, while embed reliably returns the real ~313KB payload.
# Probing the pages that get stubbed would mark the whole pool blocked.
BLOCK_PROBE_URL = os.getenv(
    "BLOCK_PROBE_URL", "https://www.tiktok.com/embed/v2/7674742042081152269")
BLOCK_PROBE_MARKER = os.getenv("BLOCK_PROBE_MARKER", "__FRONTITY_CONNECT_STATE__")
# Anything smaller than this is a stub/interstitial, not a real TikTok page. The
# ~44KB JS shell TikTok often serves is NOT a block: the extractor still gets the
# data from the embed endpoint, so only genuinely tiny bodies count.
BLOCK_MIN_BODY_BYTES = int(os.getenv("BLOCK_MIN_BODY_BYTES", "5000"))
# If this share of the pool looks blocked at once, distrust the probe rather than
# the pool: exits spread over three providers and six countries do not all get
# blocked in the same instant, but a stale probe URL breaks for all of them at
# once. Fail open, because marking everything blocked empties the pool.
BLOCK_SANITY_RATIO = float(os.getenv("BLOCK_SANITY_RATIO", "0.8"))

_probe_task = None
_block_task = None
_consecutive_failures = {}
_block_state = {}


def _all_proxy_urls():
    urls = []
    seen = set()
    for u in get_proxy_pool() + get_geo_proxies() + get_indo_proxies():
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


async def probe_proxy(proxy_url: str) -> bool:
    from curl_cffi.requests import AsyncSession

    try:
        async with AsyncSession(
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=PROBE_TIMEOUT_SECONDS,
        ) as session:
            resp = await session.get(
                "https://1.1.1.1/cdn-cgi/trace",
                timeout=PROBE_TIMEOUT_SECONDS,
                headers={"User-Agent": "curl/8"},
            )
            ok = resp.status_code == 200
    except Exception:
        ok = False

    if ok:
        _consecutive_failures.pop(proxy_url, None)
        mark_proxy_usable(proxy_url)
    else:
        n = _consecutive_failures.get(proxy_url, 0) + 1
        _consecutive_failures[proxy_url] = n
        if n >= PROBE_DEAD_AFTER:
            mark_proxy_dead(proxy_url)
    return ok


def classify_block_response(status_code: int, location: str, body_len: int, body_head: str,
                            marker_present: bool = False) -> bool:
    """True when TikTok is refusing this exit IP rather than serving the page."""
    if status_code in (403, 429):
        return True
    # Region interstitials: TikTok 302s exits it will not serve (e.g. HK -> /hk/about).
    if 300 <= status_code < 400 and "/about" in (location or ""):
        return True
    if status_code != 200:
        return True
    # WAF verdict embedded in the payload outranks a healthy-looking body.
    if '"statusCode":10204' in body_head or '"statusCode": 10204' in body_head:
        return True
    # Real data came back, so the exit is definitely being served.
    if marker_present:
        return False
    return body_len < BLOCK_MIN_BODY_BYTES


async def probe_block(proxy_url: str):
    """
    Probe TikTok through one proxy.

    Returns True (blocked), False (serving), or None when the request itself
    failed -- that is the liveness prober's business, and guessing here would
    mislabel a flaky tunnel as a blocked IP.
    """
    from curl_cffi.requests import AsyncSession

    try:
        async with AsyncSession(
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=BLOCK_PROBE_TIMEOUT_SECONDS,
        ) as session:
            resp = await session.get(
                BLOCK_PROBE_URL,
                timeout=BLOCK_PROBE_TIMEOUT_SECONDS,
                impersonate="chrome120",
                allow_redirects=False,
            )
            body = resp.text or ""
            return classify_block_response(
                resp.status_code,
                resp.headers.get("location", ""),
                len(body),
                body[:20000],
                BLOCK_PROBE_MARKER in body,
            )
    except Exception:
        return None


def apply_block_verdicts(verdicts: dict) -> bool:
    """
    Commit a probe cycle's verdicts. Returns False and commits nothing when the
    result trips the sanity ratio, since a probe that condemns nearly the whole
    pool is far more likely to be broken than the pool is to be blocked.
    """
    if not verdicts:
        return False

    blocked_count = sum(1 for v in verdicts.values() if v)
    if blocked_count / len(verdicts) > BLOCK_SANITY_RATIO:
        print(f"[BlockProbe] IGNORED: {blocked_count}/{len(verdicts)} exits looked blocked, "
              f"which means the probe is broken, not the pool. Failing open.", flush=True)
        for url in verdicts:
            clear_proxy_blocked(url)
            _block_state[url] = False
        return False

    for url, blocked in verdicts.items():
        if blocked:
            mark_proxy_blocked(url)
            if not _block_state.get(url):
                print(f"[BlockProbe] {url} is now BLOCKED by TikTok", flush=True)
        else:
            clear_proxy_blocked(url)
            if _block_state.get(url):
                print(f"[BlockProbe] {url} is serving again", flush=True)
        _block_state[url] = blocked
    return True


def block_state_snapshot():
    return dict(_block_state)


def _acquire_block_lease(ttl: int) -> bool:
    """Only one worker should probe TikTok; the others skip this cycle."""
    from proxy_state import _redis_client  # noqa: PLC0415 - shared client, set at import

    if _redis_client is None:
        return True
    try:
        return bool(_redis_client.set("proxy:blockprobe:lease", str(time.time()), nx=True, ex=ttl))
    except Exception:
        return True


async def _block_prober_loop():
    sem = asyncio.Semaphore(BLOCK_PROBE_CONCURRENCY)

    async def limited(url):
        async with sem:
            return await probe_block(url)

    while True:
        try:
            lease_ttl = max(int(BLOCK_PROBE_INTERVAL_SECONDS) - 5, 30)
            if _acquire_block_lease(lease_ttl):
                urls = _all_proxy_urls()
                if urls:
                    results = await asyncio.gather(*(limited(u) for u in urls),
                                                   return_exceptions=True)
                    verdicts = {
                        url: bool(res)
                        for url, res in zip(urls, results)
                        if isinstance(res, bool)
                    }
                    if apply_block_verdicts(verdicts):
                        blocked = sum(1 for v in verdicts.values() if v)
                        print(f"[BlockProbe] cycle done: {blocked}/{len(verdicts)} exits blocked "
                              f"({len(urls) - len(verdicts)} inconclusive)", flush=True)
        except Exception as e:
            print(f"[BlockProbe] cycle failed: {e}", flush=True)
        await asyncio.sleep(BLOCK_PROBE_INTERVAL_SECONDS)


async def _prober_loop():
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def limited(url):
        async with sem:
            return await probe_proxy(url)

    while True:
        try:
            urls = _all_proxy_urls()
            if urls:
                await asyncio.gather(*(limited(u) for u in urls), return_exceptions=True)
        except Exception as e:
            print(f"[Prober] probe cycle failed: {e}", flush=True)
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)


def start_prober():
    global _probe_task, _block_task
    if _probe_task is None:
        _probe_task = asyncio.create_task(_prober_loop())
        print("[Prober] background proxy health prober started", flush=True)
    if _block_task is None:
        _block_task = asyncio.create_task(_block_prober_loop())
        print("[BlockProbe] background TikTok block prober started", flush=True)


async def stop_prober():
    global _probe_task, _block_task
    for name in ("_probe_task", "_block_task"):
        task = globals()[name]
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            globals()[name] = None
