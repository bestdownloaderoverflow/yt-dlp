"""Centralized error mapping for yt-dlp exceptions."""

from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException

RATE_LIMIT_RETRY_AFTER = 300

RATE_LIMIT_PATTERNS = (
    "rate-limited",
    "rate limited",
    "try again later",
    "too many requests",
    "session has been rate-limited",
    "not a bot",
    "sign in to confirm",
)

NOT_FOUND_PATTERNS = (
    "unsupported url",
    "unable to download webpage",
    "not found",
)

DNS_FAILURE_PATTERNS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
)

GEO_RESTRICTED_PATTERNS = (
    "not available in your region",
    "not available in your country",
    "region-blocked",
    "geo-restricted",
    "georestricted",
    "geoblocked",
)


def _iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]

    while stack:
        current = stack.pop()
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        yield current

        cause = getattr(current, "cause", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        if isinstance(getattr(current, "__cause__", None), BaseException):
            stack.append(current.__cause__)
        if isinstance(getattr(current, "__context__", None), BaseException):
            stack.append(current.__context__)

        exc_info = getattr(current, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1 and isinstance(exc_info[1], BaseException):
            stack.append(exc_info[1])


def extract_http_status(exc: BaseException) -> Optional[int]:
    for current in _iter_exception_chain(exc):
        status = getattr(current, "status", None)
        if isinstance(status, int):
            return status

        response = getattr(current, "response", None)
        if response is None:
            continue
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value
    return None


def _is_rate_limited_message(message: str) -> bool:
    lower = message.lower()
    if any(pattern in lower for pattern in RATE_LIMIT_PATTERNS):
        return True
    return "rate" in lower and "limit" in lower


def _is_not_found_message(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in NOT_FOUND_PATTERNS)


def is_dns_failure_message(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in DNS_FAILURE_PATTERNS)


def is_geo_restricted_message(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in GEO_RESTRICTED_PATTERNS)


def _rate_limited_exception(message: str) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": "RATE_LIMITED",
            "message": message,
            "retry_after": RATE_LIMIT_RETRY_AFTER,
        },
        headers={"Retry-After": str(RATE_LIMIT_RETRY_AFTER)},
    )


def map_yt_dlp_exception(
    exc: BaseException,
    *,
    default_status: int = 400,
    enable_not_found_heuristics: bool = False,
    forbidden_detail: str | None = None,
) -> HTTPException:
    message = str(exc)
    status = extract_http_status(exc)

    if status == 429 or _is_rate_limited_message(message):
        return _rate_limited_exception(message)

    if status == 403:
        return HTTPException(
            status_code=403,
            detail=forbidden_detail or message,
        )

    if status == 404 or (enable_not_found_heuristics and _is_not_found_message(message)):
        return HTTPException(
            status_code=404,
            detail="Video not found. Please check the URL.",
        )

    return HTTPException(status_code=default_status, detail=message)


def map_generic_exception(exc: BaseException, *, default_status: int = 500) -> HTTPException:
    message = str(exc)
    if _is_rate_limited_message(message):
        return _rate_limited_exception(message)
    return HTTPException(status_code=default_status, detail=message)
