"""Per-proxy runtime state shared across workers via Redis (fallback: in-memory).

States:
  - dead:      proxy tunnel failed a health probe (unusable for N seconds)
  - cooldown:  proxy caused a request failure (parked for N seconds)
  - blocked:   the tunnel is fine but TikTok is refusing this exit IP
  - in-flight: active request count per worker (in-memory only)

The background prober (proxy_health.py) marks proxies dead/usable so the
request path never wastes user time on a broken tunnel, and separately marks
them blocked/unblocked so a healthy-but-refused exit IP stops being picked.
"""

import hashlib
import os
import threading
import time
from typing import Dict, Optional

REDIS_URL = os.getenv("REDIS_URL", "")

_redis_client = None
if REDIS_URL:
    try:
        import redis
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        print(f"[ProxyState] Redis unavailable, falling back to in-memory: {e}", flush=True)
        _redis_client = None

_in_memory_until: Dict[str, float] = {}
_in_flight: Dict[str, int] = {}
_lock = threading.Lock()

DEAD_TTL_SECONDS = int(os.getenv("PROXY_DEAD_TTL", "120"))
COOLDOWN_SECONDS = int(os.getenv("PROXY_COOLDOWN", "60"))
BLOCKED_TTL_SECONDS = int(os.getenv("PROXY_BLOCKED_TTL", "300"))
# Safety net for counters orphaned by a worker that died mid-request: the key is
# refreshed on every increment, so it only expires once a proxy has been idle
# this long. Must comfortably exceed the slowest single extraction.
IN_FLIGHT_TTL_SECONDS = int(os.getenv("PROXY_IN_FLIGHT_TTL", "600"))


def _key(proxy_url: str, kind: str) -> str:
    h = hashlib.sha256(proxy_url.strip().encode()).hexdigest()[:24]
    return f"proxy:{kind}:{h}"


def _redis_get(name: str) -> Optional[float]:
    if _redis_client is None:
        return None
    try:
        raw = _redis_client.get(name)
        return float(raw) if raw else None
    except Exception:
        return None


def _redis_set(name: str, until: float, ttl: int):
    if _redis_client is None:
        return
    try:
        _redis_client.setex(name, max(ttl, 1), str(until))
    except Exception:
        pass


def _redis_del(name: str):
    if _redis_client is None:
        return
    try:
        _redis_client.delete(name)
    except Exception:
        pass


def _set_until(kind: str, proxy_url: str, seconds: int):
    if not proxy_url:
        return
    name = _key(proxy_url, kind)
    until = time.time() + seconds
    _redis_set(name, until, seconds)
    with _lock:
        _in_memory_until[name] = until
        if len(_in_memory_until) > 500:
            now = time.time()
            for k in [k for k, v in _in_memory_until.items() if v < now]:
                _in_memory_until.pop(k, None)


def _get_until(kind: str, proxy_url: str) -> float:
    name = _key(proxy_url, kind)
    v = _redis_get(name)
    if v is not None:
        return v
    with _lock:
        return _in_memory_until.get(name) or 0.0


def _clear(kind: str, proxy_url: str):
    name = _key(proxy_url, kind)
    _redis_del(name)
    with _lock:
        _in_memory_until.pop(name, None)


def is_proxy_usable(proxy_url: str) -> bool:
    if not proxy_url:
        return True
    now = time.time()
    return (
        _get_until("dead", proxy_url) < now
        and _get_until("cooldown", proxy_url) < now
        and _get_until("blocked", proxy_url) < now
    )


def mark_proxy_dead(proxy_url: str, seconds: int = DEAD_TTL_SECONDS):
    _set_until("dead", proxy_url, seconds)


def mark_proxy_cooldown(proxy_url: str, seconds: int = COOLDOWN_SECONDS):
    _set_until("cooldown", proxy_url, seconds)


def mark_proxy_blocked(proxy_url: str, seconds: int = BLOCKED_TTL_SECONDS):
    """TikTok is refusing this exit IP even though the tunnel itself works."""
    _set_until("blocked", proxy_url, seconds)


def clear_proxy_blocked(proxy_url: str):
    _clear("blocked", proxy_url)


def is_proxy_blocked(proxy_url: str) -> bool:
    if not proxy_url:
        return False
    return _get_until("blocked", proxy_url) >= time.time()


def mark_proxy_usable(proxy_url: str):
    if not proxy_url:
        return
    _clear("dead", proxy_url)
    _clear("cooldown", proxy_url)
    _clear("blocked", proxy_url)


def proxy_status(proxy_url: str) -> Dict[str, object]:
    """Point-in-time view of one proxy, for the /proxies monitoring endpoint."""
    now = time.time()
    dead_until = _get_until("dead", proxy_url)
    cooldown_until = _get_until("cooldown", proxy_url)
    blocked_until = _get_until("blocked", proxy_url)
    return {
        "proxy": proxy_url,
        "usable": is_proxy_usable(proxy_url),
        "dead": dead_until >= now,
        "cooldown": cooldown_until >= now,
        "blocked": blocked_until >= now,
        "blocked_for_seconds": max(0, int(blocked_until - now)),
        "in_flight": get_in_flight(proxy_url),
    }


def inc_in_flight(proxy_url: str):
    """
    Claim a slot on a proxy. Shared through Redis so least-loaded selection sees
    the whole service: with several Granian workers, a per-process counter makes
    every worker think a proxy busy in another worker is idle.
    """
    if not proxy_url:
        return
    if _redis_client is not None:
        try:
            name = _key(proxy_url, "inflight")
            pipe = _redis_client.pipeline()
            pipe.incr(name)
            pipe.expire(name, IN_FLIGHT_TTL_SECONDS)
            pipe.execute()
            return
        except Exception:
            pass
    with _lock:
        _in_flight[proxy_url] = _in_flight.get(proxy_url, 0) + 1


def dec_in_flight(proxy_url: str):
    if not proxy_url:
        return
    if _redis_client is not None:
        try:
            name = _key(proxy_url, "inflight")
            # A counter can only be driven negative by a lost increment (worker
            # restart between inc and dec); clamp rather than let a proxy look
            # permanently idle and soak up every request.
            if _redis_client.decr(name) <= 0:
                _redis_client.delete(name)
            return
        except Exception:
            pass
    with _lock:
        v = _in_flight.get(proxy_url, 0) - 1
        if v <= 0:
            _in_flight.pop(proxy_url, None)
        else:
            _in_flight[proxy_url] = v


def get_in_flight(proxy_url: str) -> int:
    if _redis_client is not None:
        try:
            raw = _redis_client.get(_key(proxy_url, "inflight"))
            return max(0, int(raw)) if raw else 0
        except Exception:
            pass
    with _lock:
        return _in_flight.get(proxy_url, 0)


def get_in_flight_many(proxy_urls) -> Dict[str, int]:
    """
    Batched form of get_in_flight for proxy selection, which reads the whole
    candidate list on every request. One MGET instead of one GET per proxy.
    """
    urls = [u for u in proxy_urls if u]
    if not urls:
        return {}
    if _redis_client is not None:
        try:
            raws = _redis_client.mget([_key(u, "inflight") for u in urls])
            return {
                u: (max(0, int(raw)) if raw else 0)
                for u, raw in zip(urls, raws)
            }
        except Exception:
            pass
    with _lock:
        return {u: _in_memory_in_flight_unlocked(u) for u in urls}


def _in_memory_in_flight_unlocked(proxy_url: str) -> int:
    return _in_flight.get(proxy_url, 0)
