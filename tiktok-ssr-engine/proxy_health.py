"""Background SOCKS5 health prober for the wireproxy pool.

Runs as an asyncio task inside each Granian worker:
  - probes every proxy in the pool through its SOCKS5 tunnel
  - marks broken tunnels dead (proxy_state) so requests never wait on them
  - clears the dead mark as soon as a tunnel answers again
"""

import asyncio
import os

from config import get_geo_proxies, get_indo_proxies, get_proxy_pool
from proxy_state import mark_proxy_dead, mark_proxy_usable

PROBE_INTERVAL_SECONDS = float(os.getenv("PROBE_INTERVAL", "45"))
PROBE_TIMEOUT_SECONDS = float(os.getenv("PROBE_TIMEOUT", "6"))
PROBE_CONCURRENCY = int(os.getenv("PROBE_CONCURRENCY", "12"))
PROBE_DEAD_AFTER = int(os.getenv("PROBE_DEAD_AFTER", "2"))

_probe_task = None
_consecutive_failures = {}


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
    global _probe_task
    if _probe_task is None:
        _probe_task = asyncio.create_task(_prober_loop())
        print("[Prober] background proxy health prober started", flush=True)


async def stop_prober():
    global _probe_task
    if _probe_task is not None:
        _probe_task.cancel()
        try:
            await _probe_task
        except asyncio.CancelledError:
            pass
        _probe_task = None
