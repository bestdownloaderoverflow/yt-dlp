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
import ipaddress
import os
import time
from typing import Optional

from config import get_geo_proxies, get_indo_proxies, get_proxy_pool, get_warp_proxies
from proxy_state import (
    clear_proxy_blocked,
    get_proxy_exit_ip,
    get_proxy_exit_ips_many,
    mark_proxy_blocked,
    mark_proxy_dead,
    mark_proxy_tunnel_alive,
    record_exit_block_strike,
    record_proxy_probe_failure,
    process_proxy_reconnect,
    request_proxy_reconnect,
    set_proxy_control_verdict,
    set_proxy_exit_ip,
)

PROBE_INTERVAL_SECONDS = float(os.getenv("PROBE_INTERVAL", "45"))
PROBE_TIMEOUT_SECONDS = float(os.getenv("PROBE_TIMEOUT", "6"))
PROBE_CONCURRENCY = int(os.getenv("PROBE_CONCURRENCY", "12"))
PROBE_DEAD_AFTER = int(os.getenv("PROBE_DEAD_AFTER", "2"))
PROBE_URLS = tuple(
    url.strip() for url in os.getenv(
        "PROBE_URLS",
        "https://1.1.1.1/cdn-cgi/trace,https://api.ipify.org",
    ).split(",") if url.strip()
)
PROBE_FAILURE_TTL_SECONDS = max(
    int(PROBE_INTERVAL_SECONDS * (PROBE_DEAD_AFTER + 2)), 60)

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
# If this share of the pool looks blocked at once, distrust the probe rather than
# the pool: exits spread over three providers and six countries do not all get
# blocked in the same instant, but a stale probe URL breaks for all of them at
# once. Fail open, because marking everything blocked empties the pool.
BLOCK_SANITY_RATIO = float(os.getenv("BLOCK_SANITY_RATIO", "0.8"))
BLOCK_CONFIRMATIONS = max(1, int(os.getenv("BLOCK_CONFIRMATIONS", "2")))
BLOCK_PROBE_STARTUP_DELAY_SECONDS = float(os.getenv("BLOCK_PROBE_STARTUP_DELAY", "30"))
BLOCK_STRIKE_TTL_SECONDS = max(int(BLOCK_PROBE_INTERVAL_SECONDS * 3), 60)
RECONNECT_SCHEDULER_INTERVAL_SECONDS = float(
    os.getenv("WARP_RECONNECT_SCHEDULER_INTERVAL",
              os.getenv("WARP_RECONNECT_DRAIN_POLL", "1")))

_probe_task = None
_block_task = None
_reconnect_task = None
_block_state = {}


def _all_proxy_urls():
    urls = []
    seen = set()
    for u in get_proxy_pool() + get_geo_proxies() + get_indo_proxies():
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def record_probe_success(proxy_url: str, exit_ip: str) -> bool:
    """Record liveness and clear an old-IP block after a successful reconnect."""
    previous_exit_ip = get_proxy_exit_ip(proxy_url)
    changed = bool(previous_exit_ip and exit_ip and previous_exit_ip != exit_ip)
    mark_proxy_tunnel_alive(proxy_url)
    if exit_ip:
        set_proxy_exit_ip(proxy_url, exit_ip)
    if changed:
        clear_proxy_blocked(proxy_url)
        record_exit_block_strike(previous_exit_ip, False)
        print(f"[Prober] {proxy_url} exit IPv4 changed "
              f"{previous_exit_ip} -> {exit_ip}; block cleared", flush=True)
    return changed


async def probe_proxy(proxy_url: str) -> bool:
    from curl_cffi.requests import AsyncSession

    ok = False
    exit_ip = ""
    try:
        async with AsyncSession(
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=PROBE_TIMEOUT_SECONDS,
        ) as session:
            for probe_url in PROBE_URLS:
                try:
                    resp = await session.get(
                        probe_url,
                        timeout=PROBE_TIMEOUT_SECONDS,
                        headers={"User-Agent": "curl/8"},
                    )
                    if resp.status_code != 200:
                        continue
                    body = (resp.text or "").strip()
                    candidate = body
                    for line in body.splitlines():
                        if line.startswith("ip="):
                            candidate = line.split("=", 1)[1].strip()
                            break
                    try:
                        exit_ip = str(ipaddress.IPv4Address(candidate))
                    except ipaddress.AddressValueError:
                        continue
                    ok = True
                    break
                except Exception:
                    continue
    except Exception:
        pass

    if ok:
        record_proxy_probe_failure(proxy_url, False)
        record_probe_success(proxy_url, exit_ip)
    else:
        n = record_proxy_probe_failure(
            proxy_url, True, ttl_seconds=PROBE_FAILURE_TTL_SECONDS)
        if n >= PROBE_DEAD_AFTER:
            mark_proxy_dead(proxy_url)
    return ok


def classify_block_response(status_code: int, location: str, body_len: int, body_head: str,
                            marker_present: bool = False) -> Optional[bool]:
    """Return True for a hard block, False for proven service, else None."""
    if status_code in (403, 429):
        return True
    # Region interstitials: TikTok 302s exits it will not serve (e.g. HK -> /hk/about).
    if 300 <= status_code < 400 and "/about" in (location or ""):
        return True
    # WAF verdict embedded in the payload outranks a healthy-looking body.
    if '"statusCode":10204' in body_head or '"statusCode": 10204' in body_head:
        return True
    # A 5xx, timeout-like response, stale probe post, or unexpected page is not
    # evidence that the public IP is blocked. Preserve the previous verdict.
    if status_code != 200:
        return None
    # Real data came back, so the exit is definitely being served.
    if marker_present:
        return False
    # Tiny stubs and large shells without the data marker are both inconclusive:
    # TikTok serves them for geo/content reasons as well as during degradation.
    return None


async def probe_block(proxy_url: str, timeout_seconds: Optional[float] = None):
    """
    Probe TikTok through one proxy.

    Returns True (blocked), False (serving), or None when the request itself
    failed -- that is the liveness prober's business, and guessing here would
    mislabel a flaky tunnel as a blocked IP.
    """
    from curl_cffi.requests import AsyncSession

    timeout = timeout_seconds or BLOCK_PROBE_TIMEOUT_SECONDS
    try:
        async with AsyncSession(
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
        ) as session:
            resp = await session.get(
                BLOCK_PROBE_URL,
                timeout=timeout,
                impersonate="chrome120",
                allow_redirects=False,
            )
            body = resp.text or ""
            verdict = classify_block_response(
                resp.status_code,
                resp.headers.get("location", ""),
                len(body),
                body[:20000],
                BLOCK_PROBE_MARKER in body,
            )
            # A serving verdict is safe to cache immediately. A blocked verdict
            # is cached only after apply_block_verdicts passes the pool-wide
            # sanity ratio and confirmation threshold.
            if verdict is False:
                set_proxy_control_verdict(proxy_url, False)
            return verdict
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

    all_urls = list(dict.fromkeys(_all_proxy_urls() + list(verdicts)))
    exit_ips = get_proxy_exit_ips_many(all_urls)
    members_by_ip = {}
    verdicts_by_ip = {}
    for url in all_urls:
        if exit_ip := exit_ips.get(url, ""):
            members_by_ip.setdefault(exit_ip, []).append(url)
    for url, verdict in verdicts.items():
        if (exit_ip := exit_ips.get(url, "")) and isinstance(verdict, bool):
            verdicts_by_ip.setdefault(exit_ip, []).append(verdict)

    # Unknown exit IPs are deliberately inconclusive. Containers sharing one
    # IPv4 form one verdict group; a proven-serving result wins over a block.
    grouped_verdicts = {
        exit_ip: (False if False in values else True)
        for exit_ip, values in verdicts_by_ip.items()
        if values
    }
    if not grouped_verdicts:
        return False

    blocked_count = sum(1 for value in grouped_verdicts.values() if value)
    if blocked_count / len(grouped_verdicts) > BLOCK_SANITY_RATIO:
        print(f"[BlockProbe] IGNORED: {blocked_count}/{len(grouped_verdicts)} unique IPv4 exits looked blocked, "
              f"which means the probe is broken, not the pool. Preserving previous state.", flush=True)
        # Preserve the last known state. A broken probe is not evidence that
        # previously blocked exits have recovered.
        return False

    for exit_ip, blocked in grouped_verdicts.items():
        members = members_by_ip.get(exit_ip, [])
        if blocked:
            strikes = record_exit_block_strike(
                exit_ip, True, ttl_seconds=BLOCK_STRIKE_TTL_SECONDS)
            if strikes < BLOCK_CONFIRMATIONS:
                print(f"[BlockProbe] hard-block strike {strikes}/{BLOCK_CONFIRMATIONS} "
                      f"for IPv4 {exit_ip}; awaiting confirmation", flush=True)
                continue
            record_exit_block_strike(exit_ip, False)
            reconnectable_proxies = set(get_proxy_pool())
            for url in members:
                set_proxy_control_verdict(url, True)
                mark_proxy_blocked(url)
                if url in reconnectable_proxies and request_proxy_reconnect(
                    url, reason="block_probe"):
                    print(f"[BlockProbe] reconnect requested for {url} "
                          f"while IPv4 {exit_ip} is blocked", flush=True)
                if not _block_state.get(url):
                    print(f"[BlockProbe] {url} is now BLOCKED by TikTok (IPv4 {exit_ip})", flush=True)
        else:
            record_exit_block_strike(exit_ip, False)
            for url in members:
                set_proxy_control_verdict(url, False)
                clear_proxy_blocked(url)
                if _block_state.get(url):
                    print(f"[BlockProbe] {url} is serving again (IPv4 {exit_ip})", flush=True)
        for url in members:
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


def _acquire_liveness_lease(ttl: int) -> bool:
    """Only one Granian worker should probe the complete tunnel pool."""
    from proxy_state import _redis_client  # noqa: PLC0415

    if _redis_client is None:
        return True
    try:
        return bool(_redis_client.set("proxy:liveness:lease", str(time.time()), nx=True, ex=ttl))
    except Exception:
        return True


def _acquire_reconnect_lease(ttl: int) -> bool:
    """One Granian worker advances Redis-backed reconnect schedules."""
    from proxy_state import _redis_client  # noqa: PLC0415

    if _redis_client is None:
        return True
    try:
        return bool(_redis_client.set("proxy:reconnect:lease", str(time.time()),
                                      nx=True, ex=max(1, ttl)))
    except Exception:
        return True


async def _block_prober_loop():
    sem = asyncio.Semaphore(BLOCK_PROBE_CONCURRENCY)

    async def limited(url):
        async with sem:
            return await probe_block(url)

    if BLOCK_PROBE_STARTUP_DELAY_SECONDS > 0:
        await asyncio.sleep(BLOCK_PROBE_STARTUP_DELAY_SECONDS)

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
                        hard_signals = sum(1 for v in verdicts.values() if v)
                        print(f"[BlockProbe] cycle done: {hard_signals}/{len(verdicts)} hard-block signals "
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
            lease_ttl = max(int(PROBE_INTERVAL_SECONDS) - 5, 15)
            if _acquire_liveness_lease(lease_ttl):
                urls = _all_proxy_urls()
                if urls:
                    await asyncio.gather(*(limited(u) for u in urls), return_exceptions=True)
        except Exception as e:
            print(f"[Prober] probe cycle failed: {e}", flush=True)
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)


async def _reconnect_scheduler_loop():
    interval = max(0.1, RECONNECT_SCHEDULER_INTERVAL_SECONDS)
    while True:
        try:
            if _acquire_reconnect_lease(max(1, int(interval))):
                for proxy_url in get_proxy_pool():
                    process_proxy_reconnect(proxy_url)
        except Exception as exc:
            print(f"[ReconnectScheduler] cycle failed: {exc}", flush=True)
        await asyncio.sleep(interval)


def start_prober():
    global _probe_task, _block_task, _reconnect_task
    if _probe_task is None:
        _probe_task = asyncio.create_task(_prober_loop())
        print("[Prober] background proxy health prober started", flush=True)
    if _block_task is None:
        _block_task = asyncio.create_task(_block_prober_loop())
        print("[BlockProbe] background TikTok block prober started", flush=True)
    if _reconnect_task is None:
        _reconnect_task = asyncio.create_task(_reconnect_scheduler_loop())
        print("[ReconnectScheduler] managed reconnect scheduler started", flush=True)


async def stop_prober():
    global _probe_task, _block_task, _reconnect_task
    for name in ("_probe_task", "_block_task", "_reconnect_task"):
        task = globals()[name]
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            globals()[name] = None
