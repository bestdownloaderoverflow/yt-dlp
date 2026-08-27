"""Small Redis-backed Prometheus counter store shared by Granian workers."""

import json
import os
import threading
from typing import Dict

REDIS_URL = os.getenv("REDIS_URL", "")
_HASH_KEY = "metrics:tiktok_ssr:counters"
_client = None
if REDIS_URL:
    try:
        import redis
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _client = None

_memory: Dict[str, int] = {}
_lock = threading.Lock()


def _field(name: str, labels: Dict[str, str]) -> str:
    return json.dumps([name, sorted((str(k), str(v)) for k, v in labels.items())],
                      separators=(",", ":"))


def inc_counter(name: str, amount: int = 1, **labels: str):
    field = _field(name, labels)
    if _client is not None:
        try:
            _client.hincrby(_HASH_KEY, field, amount)
            return
        except Exception:
            pass
    with _lock:
        _memory[field] = _memory.get(field, 0) + amount


def counter_snapshot() -> Dict[str, int]:
    combined: Dict[str, int] = {}
    if _client is not None:
        try:
            combined.update({key: int(value) for key, value in _client.hgetall(_HASH_KEY).items()})
        except Exception:
            pass
    with _lock:
        for key, value in _memory.items():
            combined[key] = combined.get(key, 0) + value
    return combined


def _label_text(labels) -> str:
    if not labels:
        return ""
    escaped = []
    for key, value in labels:
        safe = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        escaped.append(f'{key}="{safe}"')
    return "{" + ",".join(escaped) + "}"


def render_prometheus(uptime_seconds: int, proxy_entries: list[dict]) -> str:
    lines = [
        "# HELP tiktok_ssr_uptime_seconds Uptime in seconds",
        "# TYPE tiktok_ssr_uptime_seconds gauge",
        f"tiktok_ssr_uptime_seconds {uptime_seconds}",
    ]
    counters = counter_snapshot()
    seen = set()
    for raw, value in sorted(counters.items()):
        try:
            name, labels = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if name not in seen:
            lines.extend((f"# HELP {name} Total {name}", f"# TYPE {name} counter"))
            seen.add(name)
        lines.append(f"{name}{_label_text(labels)} {value}")

    gauge_names = (
        ("tiktok_proxy_usable", "usable"),
        ("tiktok_proxy_dead", "dead"),
        ("tiktok_proxy_blocked", "blocked"),
        ("tiktok_proxy_draining", "draining"),
        ("tiktok_proxy_restart_scheduled", "restart_scheduled"),
        ("tiktok_proxy_reconnecting", "reconnecting"),
        ("tiktok_proxy_stabilizing", "stabilizing"),
        ("tiktok_proxy_quarantined", "quarantined"),
        ("tiktok_proxy_restart_backoff_seconds", "restart_backoff_for_seconds"),
        ("tiktok_proxy_quarantine_seconds", "quarantine_for_seconds"),
        ("tiktok_proxy_in_flight", "in_flight"),
    )
    for metric_name, state_key in gauge_names:
        lines.extend((f"# HELP {metric_name} Current proxy {state_key} state",
                      f"# TYPE {metric_name} gauge"))
        for entry in proxy_entries:
            proxy = str(entry.get("proxy", ""))
            value = entry.get(state_key, 0)
            if isinstance(value, bool):
                value = 1 if value else 0
            lines.append(f'{metric_name}{{proxy="{proxy}"}} {int(value or 0)}')
    return "\n".join(lines) + "\n"
