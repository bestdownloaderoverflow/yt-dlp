import json
import secrets
import time
from typing import Any, Dict, Optional
from config import REDIS_URL

_redis_client = None

if REDIS_URL:
    try:
        import redis
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        print(f"[Session] Redis connection error: {e}, falling back to in-memory store")
        _redis_client = None

# In-memory fallback store: key -> (data, expire_at)
_in_memory_sessions: Dict[str, tuple[Dict[str, Any], float]] = {}
SESSION_TTL_SECONDS = 3600  # 1 hour


def _cleanup_expired():
    now = time.time()
    expired = [k for k, (_, exp) in _in_memory_sessions.items() if exp < now]
    for k in expired:
        _in_memory_sessions.pop(k, None)


def create_session(data: Dict[str, Any]) -> str:
    key = secrets.token_urlsafe(24)
    if _redis_client:
        try:
            _redis_client.setex(f"session:{key}", SESSION_TTL_SECONDS, json.dumps(data))
            return key
        except Exception:
            pass

    _cleanup_expired()
    _in_memory_sessions[key] = (data, time.time() + SESSION_TTL_SECONDS)
    return key


def get_session(key: str) -> Optional[Dict[str, Any]]:
    if not key:
        return None

    if _redis_client:
        try:
            raw = _redis_client.get(f"session:{key}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    _cleanup_expired()
    item = _in_memory_sessions.get(key)
    if item:
        data, exp = item
        if exp >= time.time():
            return data
        _in_memory_sessions.pop(key, None)
    return None


import hashlib

def get_cached_extraction(url: str) -> Optional[Dict[str, Any]]:
    if not url:
        return None
    url_hash = hashlib.sha256(url.strip().encode()).hexdigest()[:32]
    if _redis_client:
        try:
            raw = _redis_client.get(f"extract:{url_hash}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    _cleanup_expired()
    item = _in_memory_sessions.get(f"extract:{url_hash}")
    if item:
        data, exp = item
        if exp >= time.time():
            return data
        _in_memory_sessions.pop(f"extract:{url_hash}", None)
    return None


def set_cached_extraction(url: str, data: Dict[str, Any], ttl: int = 300):
    if not url or not data:
        return
    url_hash = hashlib.sha256(url.strip().encode()).hexdigest()[:32]
    if _redis_client:
        try:
            _redis_client.setex(f"extract:{url_hash}", ttl, json.dumps(data))
            return
        except Exception:
            pass

    _cleanup_expired()
    _in_memory_sessions[f"extract:{url_hash}"] = (data, time.time() + ttl)

