"""Per-proxy runtime state shared across workers via Redis (fallback: in-memory).

States:
  - dead:      proxy tunnel failed a health probe (unusable for N seconds)
  - cooldown:  proxy caused a request failure (parked for N seconds)
  - blocked:   the tunnel is fine but TikTok is refusing this exit IP
  - in-flight: active request count shared in Redis (in-memory fallback)

The background prober (proxy_health.py) marks proxies dead/usable so the
request path never wastes user time on a broken tunnel, and separately marks
them blocked/unblocked so a healthy-but-refused exit IP stops being picked.
"""

import hashlib
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

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
_in_memory_exit_ips: Dict[str, str] = {}
_in_memory_counters: Dict[str, int] = {}
_in_memory_strings: Dict[str, tuple[str, float]] = {}
_in_flight: Dict[str, int] = {}
_lock = threading.Lock()

DEAD_TTL_SECONDS = int(os.getenv("PROXY_DEAD_TTL", "120"))
COOLDOWN_SECONDS = int(os.getenv("PROXY_COOLDOWN", "60"))
BLOCKED_TTL_SECONDS = int(os.getenv("PROXY_BLOCKED_TTL", "300"))
EXIT_IP_TTL_SECONDS = int(os.getenv("PROXY_EXIT_IP_TTL", "600"))
RECONNECT_SIGNAL_DIR = os.getenv("WIREPROXY_RECONNECT_DIR", "")
RECONNECT_COOLDOWN_SECONDS = int(os.getenv("WARP_RECONNECT_COOLDOWN", "300"))
RECONNECT_DRAIN_TIMEOUT_SECONDS = float(os.getenv("WARP_RECONNECT_DRAIN_TIMEOUT", "60"))
RECONNECT_DRAIN_POLL_SECONDS = float(os.getenv("WARP_RECONNECT_DRAIN_POLL", "1"))
RECONNECT_VERIFY_TIMEOUT_SECONDS = int(os.getenv("WARP_RECONNECT_VERIFY_TIMEOUT", "60"))
RECONNECT_STABILIZE_SECONDS = float(os.getenv("WARP_RECONNECT_STABILIZE", "2"))
RECONNECT_BACKOFF_BASE_SECONDS = int(os.getenv("WARP_RECONNECT_BACKOFF_BASE", "30"))
RECONNECT_BACKOFF_MAX_SECONDS = int(os.getenv("WARP_RECONNECT_BACKOFF_MAX", "300"))
RECONNECT_BACKOFF_JITTER_SECONDS = int(os.getenv("WARP_RECONNECT_BACKOFF_JITTER", "5"))
RECONNECT_BUDGET_LIMIT = int(os.getenv("WARP_RECONNECT_BUDGET_LIMIT", "3"))
RECONNECT_BUDGET_WINDOW_SECONDS = int(os.getenv("WARP_RECONNECT_BUDGET_WINDOW", "600"))
RECONNECT_QUARANTINE_SECONDS = int(os.getenv("WARP_RECONNECT_QUARANTINE", "600"))
RECONNECT_PENDING_TTL_SECONDS = int(os.getenv("WARP_RECONNECT_PENDING_TTL", "86400"))
# Safety net for counters orphaned by a worker that died mid-request: the key is
# refreshed on every increment, so it only expires once a proxy has been idle
# this long. Must comfortably exceed the slowest single extraction.
IN_FLIGHT_TTL_SECONDS = int(os.getenv("PROXY_IN_FLIGHT_TTL", "600"))


def _key(proxy_url: str, kind: str) -> str:
    h = hashlib.sha256(proxy_url.strip().encode()).hexdigest()[:24]
    return f"proxy:{kind}:{h}"


def _reconnect_metric(result: str, reason: str = "unknown"):
    try:
        from service_metrics import inc_counter
        inc_counter("tiktok_proxy_reconnect_total", result=result, reason=reason or "unknown")
    except Exception:
        pass


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
        _redis_client.set(name, str(until), ex=max(ttl, 1))
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


def _acquire_until(kind: str, proxy_url: str, seconds: int) -> bool:
    """Atomically acquire a time-bounded gate across workers."""
    if not proxy_url:
        return False
    name = _key(proxy_url, kind)
    ttl = max(1, int(seconds))
    until = time.time() + ttl
    if _redis_client is not None:
        try:
            if not _redis_client.set(name, str(until), nx=True, ex=ttl):
                return False
            with _lock:
                _in_memory_until[name] = until
            return True
        except Exception:
            pass
    with _lock:
        if _in_memory_until.get(name, 0) >= time.time():
            return False
        _in_memory_until[name] = until
        return True


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
        and _get_until("reconnect_pending", proxy_url) < now
        and _get_until("reconnecting", proxy_url) < now
        and _get_until("quarantine", proxy_url) < now
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


def clear_proxy_dead(proxy_url: str):
    """Clear tunnel liveness state without touching TikTok-specific state."""
    _clear("dead", proxy_url)


def clear_proxy_cooldown(proxy_url: str):
    """Clear request cooldown without changing dead/blocked verdicts."""
    _clear("cooldown", proxy_url)


def is_proxy_blocked(proxy_url: str) -> bool:
    if not proxy_url:
        return False
    return _get_until("blocked", proxy_url) >= time.time()


def mark_proxy_usable(proxy_url: str):
    """Mark fully usable after a successful official TikTok request."""
    if not proxy_url:
        return
    _clear("dead", proxy_url)
    _clear("cooldown", proxy_url)
    _clear("blocked", proxy_url)


def mark_proxy_tunnel_alive(proxy_url: str):
    """A generic liveness probe proves only that the tunnel works."""
    clear_proxy_dead(proxy_url)


def mark_proxy_request_success(proxy_url: str, *, tiktok_served: bool):
    """Clear request failures; clear block only when TikTok served official data."""
    if not proxy_url:
        return
    clear_proxy_dead(proxy_url)
    clear_proxy_cooldown(proxy_url)
    if tiktok_served:
        clear_proxy_blocked(proxy_url)
        if exit_ip := get_proxy_exit_ip(proxy_url):
            record_exit_block_strike(exit_ip, False)


def set_proxy_exit_ip(proxy_url: str, exit_ip: str):
    if not proxy_url or not exit_ip:
        return
    name = _key(proxy_url, "exitip")
    _set_string("exitip_seen_at", proxy_url, str(time.time()), EXIT_IP_TTL_SECONDS)
    if _redis_client is not None:
        try:
            _redis_client.set(name, exit_ip, ex=EXIT_IP_TTL_SECONDS)
            return
        except Exception:
            pass
    with _lock:
        _in_memory_exit_ips[name] = exit_ip


def get_proxy_exit_ip(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    name = _key(proxy_url, "exitip")
    if _redis_client is not None:
        try:
            return _redis_client.get(name) or ""
        except Exception:
            pass
    with _lock:
        return _in_memory_exit_ips.get(name, "")


def clear_proxy_exit_ip(proxy_url: str):
    if not proxy_url:
        return
    name = _key(proxy_url, "exitip")
    if _redis_client is not None:
        try:
            _redis_client.delete(name)
        except Exception:
            pass
    with _lock:
        _in_memory_exit_ips.pop(name, None)
    _clear_string("exitip_seen_at", proxy_url)


def get_proxy_exit_ips_many(proxy_urls) -> Dict[str, str]:
    urls = [u for u in proxy_urls if u]
    if not urls:
        return {}
    if _redis_client is not None:
        try:
            values = _redis_client.mget([_key(u, "exitip") for u in urls])
            return {u: (value or "") for u, value in zip(urls, values)}
        except Exception:
            pass
    return {u: get_proxy_exit_ip(u) for u in urls}


def record_exit_block_strike(exit_ip: str, blocked: bool, ttl_seconds: int = 900) -> int:
    """Persist hard-block confirmation count per public exit IPv4."""
    if not exit_ip:
        return 0
    name = _key(f"exit:{exit_ip}", "blockstrike")
    if _redis_client is not None:
        try:
            if not blocked:
                _redis_client.delete(name)
                return 0
            pipe = _redis_client.pipeline()
            pipe.incr(name)
            pipe.expire(name, max(1, ttl_seconds))
            values = pipe.execute()
            return int(values[0] or 0)
        except Exception:
            pass
    with _lock:
        if not blocked:
            _in_memory_counters.pop(name, None)
            return 0
        value = _in_memory_counters.get(name, 0) + 1
        _in_memory_counters[name] = value
        return value


def record_proxy_probe_failure(proxy_url: str, failed: bool, ttl_seconds: int = 300) -> int:
    """Track consecutive liveness failures consistently across Granian workers."""
    if not proxy_url:
        return 0
    name = _key(proxy_url, "probefail")
    if _redis_client is not None:
        try:
            if not failed:
                _redis_client.delete(name)
                return 0
            pipe = _redis_client.pipeline()
            pipe.incr(name)
            pipe.expire(name, max(1, ttl_seconds))
            values = pipe.execute()
            return int(values[0] or 0)
        except Exception:
            pass
    with _lock:
        if not failed:
            _in_memory_counters.pop(name, None)
            return 0
        value = _in_memory_counters.get(name, 0) + 1
        _in_memory_counters[name] = value
        return value


def _set_string(kind: str, proxy_url: str, value: str, ttl: int):
    name = _key(proxy_url, kind)
    if _redis_client is not None:
        try:
            _redis_client.set(name, value, ex=max(1, ttl))
            return
        except Exception:
            pass
    with _lock:
        _in_memory_strings[name] = (value, time.time() + max(1, ttl))


def _get_string(kind: str, proxy_url: str) -> str:
    name = _key(proxy_url, kind)
    if _redis_client is not None:
        try:
            return _redis_client.get(name) or ""
        except Exception:
            pass
    with _lock:
        item = _in_memory_strings.get(name)
        if not item:
            return ""
        value, expires = item
        if expires < time.time():
            _in_memory_strings.pop(name, None)
            return ""
        return value


def _clear_string(kind: str, proxy_url: str):
    name = _key(proxy_url, kind)
    _redis_del(name)
    with _lock:
        _in_memory_strings.pop(name, None)


def _increment_window_counter(kind: str, proxy_url: str, window_seconds: int) -> int:
    name = _key(proxy_url, kind)
    if _redis_client is not None:
        try:
            pipe = _redis_client.pipeline()
            pipe.incr(name)
            pipe.expire(name, max(1, window_seconds))
            values = pipe.execute()
            return int(values[0] or 0)
        except Exception:
            pass
    now = time.time()
    expiry_key = f"{name}:expires"
    with _lock:
        if _in_memory_until.get(expiry_key, 0) < now:
            _in_memory_counters[name] = 0
        value = _in_memory_counters.get(name, 0) + 1
        _in_memory_counters[name] = value
        _in_memory_until[expiry_key] = now + max(1, window_seconds)
        return value


def _clear_window_counter(kind: str, proxy_url: str):
    name = _key(proxy_url, kind)
    _redis_del(name)
    with _lock:
        _in_memory_counters.pop(name, None)
        _in_memory_until.pop(f"{name}:expires", None)


def _write_reconnect_marker(hostname: str) -> bool:
    marker_dir = Path(RECONNECT_SIGNAL_DIR)
    marker = marker_dir / hostname
    temporary = marker_dir / f".{hostname}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_text(f"{time.time_ns()}\n", encoding="ascii")
        temporary.replace(marker)
        return True
    except OSError as exc:
        print(f"[ProxyState] reconnect signal failed for {hostname}: {exc}", flush=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def request_proxy_reconnect(
    proxy_url: str,
    cooldown_seconds: int = RECONNECT_COOLDOWN_SECONDS,
    drain_timeout_seconds: float = RECONNECT_DRAIN_TIMEOUT_SECONDS,
    drain_poll_seconds: float = RECONNECT_DRAIN_POLL_SECONDS,
    reason: str = "unknown",
) -> bool:
    """Persist a reconnect request for the managed background scheduler.

    The engine deliberately does not get access to the Docker socket. WARP
    containers watch their own marker in a shared volume and restart only the
    WireProxy process when the marker changes. Redis makes the schedule survive
    a Granian worker exit; reconnect processing is performed under one lease.

    cooldown_seconds and drain_poll_seconds remain accepted for API compatibility;
    backoff and scheduler cadence are configured independently.
    """
    if not proxy_url or not RECONNECT_SIGNAL_DIR:
        return False
    hostname = (urlsplit(proxy_url).hostname or "").lower()
    if not re.fullmatch(r"wireproxy-\d+", hostname):
        return False

    del cooldown_seconds, drain_poll_seconds
    now = time.time()
    if (_get_until("reconnecting", proxy_url) >= now
            or _get_until("quarantine", proxy_url) >= now):
        _reconnect_metric("deduplicated", _get_string("reconnect_reason", proxy_url) or reason)
        return False
    if not _acquire_until("reconnect_pending", proxy_url, RECONNECT_PENDING_TTL_SECONDS):
        _reconnect_metric("deduplicated", _get_string("reconnect_reason", proxy_url) or reason)
        return False
    _set_until("drain_deadline", proxy_url, max(1, int(drain_timeout_seconds)))
    _clear_string("reconnect_signaled", proxy_url)
    _set_string("reconnect_old_ip", proxy_url, get_proxy_exit_ip(proxy_url),
                RECONNECT_PENDING_TTL_SECONDS)
    _set_string("reconnect_reason", proxy_url, reason or "unknown",
                RECONNECT_PENDING_TTL_SECONDS)
    _reconnect_metric("scheduled", reason)
    return True


def reconnect_state(proxy_url: str) -> Dict[str, object]:
    now = time.time()
    pending_until = _get_until("reconnect_pending", proxy_url)
    reconnecting_until = _get_until("reconnecting", proxy_url)
    backoff_until = _get_until("reconnect_backoff", proxy_url)
    quarantine_until = _get_until("quarantine", proxy_url)
    try:
        recovered_at = float(_get_string("reconnect_recovered_at", proxy_url) or 0)
    except ValueError:
        recovered_at = 0
    return {
        "restart_scheduled": pending_until >= now,
        "reconnecting": reconnecting_until >= now,
        "stabilizing": reconnecting_until >= now and recovered_at > 0,
        "draining": pending_until >= now and reconnecting_until < now and get_in_flight(proxy_url) > 0,
        "drain_for_seconds": max(0, int(_get_until("drain_deadline", proxy_url) - now)),
        "restart_backoff_for_seconds": max(0, int(backoff_until - now)),
        "quarantined": quarantine_until >= now,
        "quarantine_for_seconds": max(0, int(quarantine_until - now)),
        "restart_reason": _get_string("reconnect_reason", proxy_url) or "",
    }


def clear_proxy_reconnect_state(proxy_url: str):
    """Clear scheduler state; intended for recovery tooling and deterministic tests."""
    for kind in ("reconnect_pending", "reconnecting", "drain_deadline",
                 "reconnect_backoff", "quarantine"):
        _clear(kind, proxy_url)
    for kind in ("reconnect_old_ip", "reconnect_signaled", "reconnect_reason",
                 "reconnect_recovered_at"):
        _clear_string(kind, proxy_url)
    _clear_window_counter("reconnect_failures", proxy_url)


def _record_reconnect_failure(proxy_url: str) -> tuple[int, int, bool]:
    failures = _increment_window_counter(
        "reconnect_failures", proxy_url, RECONNECT_BUDGET_WINDOW_SECONDS)
    if failures >= max(1, RECONNECT_BUDGET_LIMIT):
        _set_until("quarantine", proxy_url, RECONNECT_QUARANTINE_SECONDS)
        return failures, RECONNECT_QUARANTINE_SECONDS, True
    exponent = max(0, min(failures - 1, 8))
    delay = min(RECONNECT_BACKOFF_MAX_SECONDS,
                RECONNECT_BACKOFF_BASE_SECONDS * (2 ** exponent))
    if RECONNECT_BACKOFF_JITTER_SECONDS > 0:
        delay += random.randint(0, RECONNECT_BACKOFF_JITTER_SECONDS)
    _set_until("reconnect_backoff", proxy_url, max(1, delay))
    return failures, delay, False


def process_proxy_reconnect(proxy_url: str) -> str:
    """Advance one persisted reconnect state; returns a metrics-friendly outcome."""
    now = time.time()
    reason = _get_string("reconnect_reason", proxy_url) or "unknown"
    if _get_until("reconnect_pending", proxy_url) < now:
        return "idle"
    if _get_until("quarantine", proxy_url) >= now:
        return "quarantined"

    reconnecting_until = _get_until("reconnecting", proxy_url)
    if reconnecting_until >= now:
        old_ip = _get_string("reconnect_old_ip", proxy_url)
        current_ip = get_proxy_exit_ip(proxy_url)
        try:
            signaled_at = float(_get_string("reconnect_signaled", proxy_url) or 0)
            exit_seen_at = float(_get_string("exitip_seen_at", proxy_url) or 0)
        except ValueError:
            signaled_at = exit_seen_at = 0
        probed_after_signal = exit_seen_at >= signaled_at > 0
        recovered = probed_after_signal and bool(
            current_ip and old_ip and current_ip != old_ip)
        recovered = recovered or (
            probed_after_signal
            and bool(current_ip)
            and not is_proxy_blocked(proxy_url)
            and _get_until("dead", proxy_url) < now
        )
        if recovered:
            recovered_at_raw = _get_string("reconnect_recovered_at", proxy_url)
            recovered_at = float(recovered_at_raw or 0)
            if recovered_at <= 0:
                _set_string("reconnect_recovered_at", proxy_url, str(now),
                            RECONNECT_PENDING_TTL_SECONDS)
                return "stabilizing"
            if now - recovered_at < max(0, RECONNECT_STABILIZE_SECONDS):
                return "stabilizing"
            for kind in ("reconnect_pending", "reconnecting", "drain_deadline",
                         "reconnect_backoff", "quarantine"):
                _clear(kind, proxy_url)
            _clear_string("reconnect_old_ip", proxy_url)
            _clear_string("reconnect_signaled", proxy_url)
            _clear_string("reconnect_reason", proxy_url)
            _clear_string("reconnect_recovered_at", proxy_url)
            _clear_window_counter("reconnect_failures", proxy_url)
            _reconnect_metric("verified", reason)
            return "verified"
        _clear_string("reconnect_recovered_at", proxy_url)
        return "verifying"

    # A reconnect was signaled but never verified before the deadline.
    if _get_string("reconnect_signaled", proxy_url):
        _clear_string("reconnect_signaled", proxy_url)
        failures, delay, quarantined = _record_reconnect_failure(proxy_url)
        if quarantined:
            print(f"[ProxyState] {proxy_url} quarantined after {failures} failed reconnects", flush=True)
            _reconnect_metric("quarantined", reason)
            return "quarantined"
        print(f"[ProxyState] {proxy_url} reconnect verification failed; "
              f"retry in {delay}s", flush=True)
        _reconnect_metric("verification_failed", reason)
        return "verification_failed"

    if _get_until("reconnect_backoff", proxy_url) >= now:
        return "backoff"

    active = get_in_flight(proxy_url)
    if active > 0:
        if _get_until("drain_deadline", proxy_url) < now:
            print(f"[ProxyState] drain timeout for {proxy_url}; reconnect postponed "
                  f"to protect {active} active request(s)", flush=True)
            _set_until("drain_deadline", proxy_url,
                       max(1, int(RECONNECT_DRAIN_TIMEOUT_SECONDS)))
            _reconnect_metric("postponed", reason)
            return "postponed"
        return "draining"

    hostname = (urlsplit(proxy_url).hostname or "").lower()
    if not _write_reconnect_marker(hostname):
        failures, _delay, quarantined = _record_reconnect_failure(proxy_url)
        _reconnect_metric("quarantined" if quarantined else "signal_failed", reason)
        return "quarantined" if quarantined else "signal_failed"
    _set_string("reconnect_signaled", proxy_url, str(time.time()),
                RECONNECT_PENDING_TTL_SECONDS)
    _set_until("reconnecting", proxy_url, RECONNECT_VERIFY_TIMEOUT_SECONDS)
    print(f"[ProxyState] {hostname} drained; reconnect signal written", flush=True)
    _reconnect_metric("signaled", reason)
    return "signaled"


def proxy_status(proxy_url: str) -> Dict[str, object]:
    """Point-in-time view of one proxy, for the /proxies monitoring endpoint."""
    now = time.time()
    dead_until = _get_until("dead", proxy_url)
    cooldown_until = _get_until("cooldown", proxy_url)
    blocked_until = _get_until("blocked", proxy_url)
    reconnect = reconnect_state(proxy_url)
    return {
        "proxy": proxy_url,
        "usable": is_proxy_usable(proxy_url),
        "dead": dead_until >= now,
        "cooldown": cooldown_until >= now,
        "blocked": blocked_until >= now,
        "blocked_for_seconds": max(0, int(blocked_until - now)),
        "in_flight": get_in_flight(proxy_url),
        "exit_ip": get_proxy_exit_ip(proxy_url),
        **reconnect,
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
