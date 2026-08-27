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


def _dehydrate_cookies(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Store the cookie string once and let sessions point at it.

    One extraction creates four to six sessions (hd, sd, watermark, mp3, photos)
    that all carry the same ~800 byte cookie blob. Measured, that duplication is
    the single largest contributor to the ~12KB each extraction costs in Redis,
    and Redis capacity -- not TikTok -- is what caps sustained throughput.
    """
    cookies = data.get("cookies")
    if not cookies or _redis_client is None:
        return data
    digest = hashlib.sha256(cookies.encode()).hexdigest()[:32]
    try:
        # Refresh the TTL on every reference so the jar outlives its sessions.
        _redis_client.set(f"cookiejar:{digest}", cookies, ex=SESSION_TTL_SECONDS)
    except Exception:
        return data
    shrunk = dict(data)
    shrunk.pop("cookies", None)
    shrunk["cookies_ref"] = digest
    return shrunk


def _rehydrate_cookies(data: Dict[str, Any]) -> Dict[str, Any]:
    ref = data.get("cookies_ref")
    if not ref or _redis_client is None:
        return data
    try:
        data["cookies"] = _redis_client.get(f"cookiejar:{ref}") or ""
    except Exception:
        data["cookies"] = ""
    return data


def create_session(data: Dict[str, Any]) -> str:
    key = secrets.token_urlsafe(24)
    if _redis_client:
        stored = _dehydrate_cookies(data)
        for attempt in range(2):
            try:
                _redis_client.set(f"session:{key}", json.dumps(stored), ex=SESSION_TTL_SECONDS)
                return key
            except Exception as e:
                if attempt == 0:
                    print(f"[Session] Redis setex failed, retrying once: {e}", flush=True)
                else:
                    print(f"[Session] Redis setex failed twice, falling back to in-memory: {e}", flush=True)

    _cleanup_expired()
    _in_memory_sessions[key] = (data, time.time() + SESSION_TTL_SECONDS)
    return key


def get_session(key: str) -> Optional[Dict[str, Any]]:
    if not key:
        return None

    if _redis_client:
        for attempt in range(2):
            try:
                raw = _redis_client.get(f"session:{key}")
                if raw:
                    return _rehydrate_cookies(json.loads(raw))
                break
            except Exception as e:
                if attempt == 0:
                    print(f"[Session] Redis get failed, retrying once: {e}", flush=True)
                else:
                    print(f"[Session] Redis get failed twice, falling back to in-memory: {e}", flush=True)

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
        for attempt in range(2):
            try:
                _redis_client.set(f"extract:{url_hash}", json.dumps(data), ex=ttl)
                return
            except Exception as e:
                if attempt == 0:
                    print(f"[Session] Redis cache set failed, retrying once: {e}", flush=True)
                else:
                    print(f"[Session] Redis cache set failed twice, falling back to in-memory: {e}", flush=True)

    _cleanup_expired()
    _in_memory_sessions[f"extract:{url_hash}"] = (data, time.time() + ttl)


def get_geo_hint(url: str) -> bool:
    """Return whether a recently proven post should start in Indonesia."""
    if not url:
        return False
    url_hash = hashlib.sha256(url.strip().encode()).hexdigest()[:32]
    key = f"geo_hint:{url_hash}"
    if _redis_client:
        try:
            return bool(_redis_client.get(key))
        except Exception:
            pass
    _cleanup_expired()
    item = _in_memory_sessions.get(key)
    if item and item[1] >= time.time():
        return bool(item[0])
    _in_memory_sessions.pop(key, None)
    return False


def set_geo_hint(url: str, ttl: int = 600):
    """Remember a successful Indonesia fallback across Granian workers."""
    if not url:
        return
    url_hash = hashlib.sha256(url.strip().encode()).hexdigest()[:32]
    key = f"geo_hint:{url_hash}"
    if _redis_client:
        try:
            _redis_client.set(key, "1", ex=max(1, ttl))
            return
        except Exception:
            pass
    _cleanup_expired()
    _in_memory_sessions[key] = (True, time.time() + max(1, ttl))
