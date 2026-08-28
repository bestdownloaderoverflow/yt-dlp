import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import tempfile
import time
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from config import (
    CACHE_TTL, DEFAULT_IMPERSONATE, DEFAULT_PROXY, HOST, MAX_ATTEMPTS, PORT,
    PROXY_COUNT, TIKTOK_API_KEY, VERBOSE_LOGS, get_geo_proxies,
    get_indo_proxies, get_next_proxy, get_proxy_country, get_proxy_pool,
    get_warp_proxies,
)
from extractor import (
    TikTokAccessRestrictedError,
    TikTokExtractError,
    TikTokIPBlockedError,
    TikTokInfraError,
    TikTokSSRExtractor,
    _classify_error,
    build_filename,
)
from proxy_state import (
    clear_proxy_blocked,
    dec_in_flight,
    get_proxy_control_verdict,
    get_proxy_exit_ip,
    inc_in_flight,
    mark_proxy_blocked,
    mark_proxy_cooldown,
    mark_proxy_request_success,
    record_exit_block_strike,
    request_proxy_reconnect,
)
from session import (
    get_cached_extraction,
    get_geo_hint,
    get_session,
    set_cached_extraction,
    set_geo_hint,
)
from service_metrics import inc_counter, render_prometheus


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from proxy_health import start_prober, stop_prober
    start_prober()
    yield
    await stop_prober()


app = FastAPI(title="TikTok Direct Async SSR Scraper", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

START_TIME = time.time()
ALLOW_DIRECT_DOWNLOAD_FALLBACK = os.getenv(
    "ALLOW_DIRECT_DOWNLOAD_FALLBACK", "1" if PROXY_COUNT == 0 else "0"
).lower() in {"1", "true", "yes"}
DOWNLOAD_PROXY_ATTEMPTS = max(1, int(os.getenv("DOWNLOAD_PROXY_ATTEMPTS", "3")))
REQUEST_BLOCK_CONFIRMATIONS = max(1, int(os.getenv("REQUEST_BLOCK_CONFIRMATIONS", "2")))
REQUEST_BLOCK_STRIKE_TTL = max(60, int(os.getenv("REQUEST_BLOCK_STRIKE_TTL", "900")))
REQUEST_BLOCK_VERIFY_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("REQUEST_BLOCK_VERIFY_TIMEOUT", "6")))
GEO_HINT_TTL_SECONDS = max(60, int(os.getenv("GEO_HINT_TTL", "600")))
MAX_EXTRACTION_TOUCHES = max(1, int(os.getenv("MAX_EXTRACTION_TOUCHES", "4")))


class TikTokRequest(BaseModel):
    url: str
    proxy: Optional[str] = None
    impersonate: Optional[str] = None


def should_retry_via_indonesia(
    lane: str, error: Exception, distinct_warp_exit_failures: int,
) -> bool:
    """Require the same content failure on two WARP exits before using Indonesia."""
    return (
        lane == "cloudflare"
        and isinstance(error, TikTokExtractError)
        and not isinstance(error, TikTokInfraError)
        and distinct_warp_exit_failures >= 2
    )


def should_retry_ip_block_via_indonesia(distinct_exit_failures: int) -> bool:
    """Treat repeated 10204 across distinct exits as a possible geo restriction."""
    return distinct_exit_failures >= 2


async def verify_ambiguous_ip_block(proxy_url: Optional[str]) -> Optional[bool]:
    """Probe neutral public TikTok content through the same exit.

    True means the exit itself is blocked, False proves TikTok serves the exit,
    and None leaves the verdict inconclusive.
    """
    if not proxy_url:
        return None
    cached_verdict = get_proxy_control_verdict(proxy_url)
    if cached_verdict is not None:
        inc_counter("tiktok_control_probe_total", result="cache_hit")
        return cached_verdict
    from proxy_health import probe_block
    verdict = await probe_block(
        proxy_url, timeout_seconds=REQUEST_BLOCK_VERIFY_TIMEOUT_SECONDS)
    inc_counter(
        "tiktok_control_probe_total",
        result="blocked" if verdict is True else "serving" if verdict is False else "inconclusive",
    )
    return verdict


def should_cooldown_download_failure(error: Exception) -> bool:
    """Only transport/proxy failures justify parking a download proxy."""
    return isinstance(_classify_error(error), TikTokInfraError)


def mark_proxy_and_shared_exit_blocked(proxy: Optional[str]) -> set[str]:
    """Park the refused proxy; fan out to a shared IPv4 only after confirmation."""
    if not proxy:
        return set()
    exit_ip = get_proxy_exit_ip(proxy)
    blocked = {proxy}
    # get_proxy_pool() is the complete set; the warp/geo/indo helpers are all
    # subranges of it, so there is nothing extra to union in here.
    all_proxies = get_proxy_pool()
    confirmed = False
    if exit_ip:
        strikes = record_exit_block_strike(
            exit_ip, True, ttl_seconds=REQUEST_BLOCK_STRIKE_TTL, source="request")
        confirmed = strikes >= REQUEST_BLOCK_CONFIRMATIONS
    if exit_ip and confirmed:
        blocked.update(
            candidate for candidate in all_proxies
            if get_proxy_exit_ip(candidate) == exit_ip
        )
        record_exit_block_strike(exit_ip, False)
    reconnectable_proxies = set(all_proxies)
    for candidate in blocked:
        mark_proxy_blocked(candidate)
        if candidate in reconnectable_proxies:
            request_proxy_reconnect(candidate, reason="request_ip_blocked")
    return blocked


def clear_proxy_and_shared_exit_blocked(proxy: Optional[str]) -> set[str]:
    """Clear only TikTok block state for every proxy sharing a proven exit IPv4."""
    if not proxy:
        return set()
    exit_ip = get_proxy_exit_ip(proxy)
    serving = {proxy}
    all_proxies = get_proxy_pool()
    if exit_ip:
        serving.update(
            candidate for candidate in all_proxies
            if get_proxy_exit_ip(candidate) == exit_ip
        )
        record_exit_block_strike(exit_ip, False)
    for candidate in serving:
        clear_proxy_blocked(candidate)
    return serving


def validate_tiktok_post_url(value: str) -> str:
    """Accept official TikTok post/short URLs only and normalize cache noise."""
    raw = (value or "").strip()
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="TikTok URL must use http or https")
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        raise HTTPException(status_code=400, detail="Only TikTok URLs are supported")

    path = parsed.path or "/"
    is_short = host in {"vm.tiktok.com", "vt.tiktok.com", "t.tiktok.com"} and path != "/"
    is_post = any(marker in path.lower() for marker in ("/video/", "/photo/", "/embed/"))
    if not is_short and not is_post:
        raise HTTPException(status_code=400, detail="TikTok URL must point to a video or photo post")
    return urlunsplit(("https", host, path.rstrip("/") or "/", parsed.query, ""))


def extraction_cache_key(url: str) -> str:
    """Tracking parameters must not create separate extraction cache entries."""
    parsed = urlsplit(url)
    if any(marker in parsed.path.lower() for marker in ("/video/", "/photo/", "/embed/")):
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    return url


def validate_requested_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    allowed = set(get_proxy_pool()) | set(get_geo_proxies()) | set(get_warp_proxies())
    if proxy not in allowed:
        raise HTTPException(status_code=400, detail="Requested proxy is not part of the configured pool")
    return proxy


def check_auth(x_api_key: Optional[str] = None, api_key_query: Optional[str] = None):
    if not TIKTOK_API_KEY:
        return
    token = x_api_key or api_key_query
    if token != TIKTOK_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API key")


@app.get("/")
async def root():
    return {
        "service": "tiktok-ssr-engine",
        "transport": "granian rust + fastapi + curl_cffi async ssr",
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
    }


@app.get("/health")
@app.get("/readyz")
@app.get("/livez")
async def health():
    return {
        "status": "healthy",
        "service": "tiktok-ssr-engine",
        "uptime_seconds": int(time.time() - START_TIME),
    }


@app.get("/metrics")
async def metrics():
    from config import get_geo_proxies, get_warp_proxies
    from proxy_state import proxy_status

    entries = [proxy_status(url) for url in get_warp_proxies() + get_geo_proxies()]
    return Response(
        content=render_prometheus(int(time.time() - START_TIME), entries),
        media_type="text/plain",
    )


@app.get("/proxies")
async def proxies_status(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None, alias="api_key"),
):
    """Per-proxy view of the pool: which exits TikTok is currently refusing."""
    check_auth(x_api_key, api_key)

    from config import get_geo_proxies, get_indo_proxies, get_warp_proxies
    from proxy_state import proxy_status

    warp = get_warp_proxies()
    indo = set(get_indo_proxies())

    entries = []
    for url in warp + get_geo_proxies():
        status = proxy_status(url)
        status["lane"] = "cloudflare" if url in warp else "geo"
        status["indonesia"] = url in indo
        # "" for WARP anycast, whose egress country is not fixed.
        status["country"] = get_proxy_country(url)
        entries.append(status)

    blocked = [e["proxy"] for e in entries if e["blocked"]]
    known_exit_ips = {e["exit_ip"] for e in entries if e.get("exit_ip")}
    warp_exit_ips = {
        e["exit_ip"] for e in entries
        if e["lane"] == "cloudflare" and e.get("exit_ip")
    }
    return {
        "total": len(entries),
        "usable": sum(1 for e in entries if e["usable"]),
        "blocked": len(blocked),
        "blocked_proxies": blocked,
        "cloudflare_usable": sum(1 for e in entries if e["lane"] == "cloudflare" and e["usable"]),
        "geo_usable": sum(1 for e in entries if e["lane"] == "geo" and e["usable"]),
        "known_unique_exit_ips": len(known_exit_ips),
        "cloudflare_unique_exit_ips": len(warp_exit_ips),
        # How concentrated the pool is. Retry evidence is only as good as this:
        # a pool where one country holds most nodes cannot prove much by trying
        # "another exit". Empty country means WARP anycast.
        "usable_by_country": {
            country: sum(
                1 for e in entries
                if e.get("country") == country and e["usable"]
            )
            for country in sorted({e.get("country") or "" for e in entries})
        },
        "proxies": entries,
    }


@app.post("/tiktok")
@app.get("/tiktok")
@app.post("/fetch")
@app.get("/fetch")
async def extract_tiktok(
    request: Request,
    url: Optional[str] = Query(None, description="TikTok post URL"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None, alias="api_key"),
):
    check_auth(x_api_key, api_key)
    inc_counter("tiktok_extract_requests_total")

    target_url = ""
    req_proxy = None
    req_impersonate = None

    if request.method == "POST":
        try:
            body = await request.json()
            target_url = (body.get("url") or "").strip()
            req_proxy = body.get("proxy")
            req_impersonate = body.get("impersonate")
        except Exception:
            pass

    if not target_url and url:
        target_url = url.strip()

    if not target_url:
        raise HTTPException(status_code=400, detail="URL is required")

    target_url = validate_tiktok_post_url(target_url)
    req_proxy = validate_requested_proxy(req_proxy)
    cache_key = extraction_cache_key(target_url)

    # 1. Check Redis / In-Memory Extraction Cache first (0.5ms response for viral videos)
    cached = get_cached_extraction(cache_key)
    if cached:
        inc_counter("tiktok_extract_cache_total", result="hit")
        if VERBOSE_LOGS:
            print(f"⚡ [CACHE HIT] Returning cached result for {target_url}", flush=True)
        return JSONResponse(content=cached)

    impersonate = req_impersonate or DEFAULT_IMPERSONATE

    max_attempts = MAX_ATTEMPTS if not req_proxy else 1
    max_touches = MAX_EXTRACTION_TOUCHES
    real_attempts = 0
    touches = 0
    prefer_indo = get_geo_hint(cache_key)
    retry_warp = False
    failed_warp_exit_ips: set[str] = set()
    failed_ip_block_exit_ips: set[str] = set()
    healthy_control_exit_ips: set[str] = set()
    indonesia_touches = 0
    last_error = None
    tried_proxies: set[str] = set()
    tried_exit_ips: set[str] = set()
    tried_countries: set[str] = set()
    indo_proxies = set(get_indo_proxies())

    if VERBOSE_LOGS:
        print(f"📥 [REQUEST] Extracting: {target_url}", flush=True)

    while real_attempts < max_attempts and touches < max_touches:
        if prefer_indo and indonesia_touches >= 2:
            break
        touches += 1
        real_attempts += 1

        if prefer_indo:
            # Region-locked content still overrides everything: only Indonesia exits
            # can see it, so Cloudflare-first does not apply once we know that.
            proxy = req_proxy or get_next_proxy(
                indo_only=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
            )
            lane = "indo"
            if not proxy:
                proxy = get_next_proxy(
                    prefer_geo=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
                )
                lane = "geo (no Indonesia available)"
        elif real_attempts == 1 or retry_warp:
            # First attempt goes through Cloudflare WARP, falling back to the geo
            # pool only when every WARP node is dead, cooling down or blocked.
            # A generic content failure gets one confirmation attempt on a
            # different WARP exit before it is treated as a possible geo lock.
            retry_warp = False
            proxy = req_proxy or get_next_proxy(
                warp_only=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
            )
            lane = "cloudflare"
            if not proxy:
                proxy = get_next_proxy(
                    prefer_geo=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
                    avoid_countries=tried_countries,
                )
                lane = "geo (no WARP available)"
        else:
            proxy = req_proxy or get_next_proxy(
                prefer_geo=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
                avoid_countries=tried_countries,
            )
            lane = "geo"
        proxy = proxy or DEFAULT_PROXY or None
        if PROXY_COUNT > 0 and not proxy:
            last_error = TikTokInfraError("No usable proxy exits available")
            break
        proxy_exit_ip = ""
        if proxy:
            tried_proxies.add(proxy)
            if proxy_exit_ip := get_proxy_exit_ip(proxy):
                tried_exit_ips.add(proxy_exit_ip)
            if proxy_country := get_proxy_country(proxy):
                tried_countries.add(proxy_country)
        if lane == "indo" and proxy in indo_proxies:
            indonesia_touches += 1

        if VERBOSE_LOGS:
            print(f"  🔄 [Attempt {real_attempts}/{max_attempts}] lane={lane} proxy={proxy}", flush=True)
        inc_in_flight(proxy)
        try:
            extractor = TikTokSSRExtractor(proxy=proxy, impersonate=impersonate)
            result = await extractor.extract(target_url)
            source = result.get("extract_source") or ""
            tiktok_served = source != "web_fallback"
            mark_proxy_request_success(proxy, tiktok_served=tiktok_served)
            if tiktok_served:
                clear_proxy_and_shared_exit_blocked(proxy)
            if lane == "indo" and prefer_indo:
                set_geo_hint(cache_key, ttl=GEO_HINT_TTL_SECONDS)
                inc_counter("tiktok_geo_hint_total", result="stored")
            if VERBOSE_LOGS:
                print(f"  ✅ [Attempt {real_attempts}] SUCCESS ({result.get('extract_source')}) - Title: {result.get('title', '')[:40]}", flush=True)

            # Save successful extraction to cache
            set_cached_extraction(cache_key, result, ttl=CACHE_TTL or 300)
            inc_counter("tiktok_extract_results_total", result="success", source=source or "unknown")
            return JSONResponse(content=result)
        except TikTokInfraError as e:
            inc_counter("tiktok_extract_attempts_total", result="infra_error")
            inc_counter("tiktok_proxy_failovers_total", reason="infra_error")
            # Proxy infrastructure failure: switch proxy WITHOUT consuming an attempt
            real_attempts -= 1
            last_error = e
            mark_proxy_cooldown(proxy, 60)
            if VERBOSE_LOGS:
                print(f"  ⚠️ [Touch {touches}] INFRA FAIL on {proxy} (attempt not consumed, cooldown 60s): {e}", flush=True)
            if req_proxy:
                break
            if lane == "cloudflare" and failed_warp_exit_ips:
                retry_warp = True
        except TikTokAccessRestrictedError as e:
            inc_counter("tiktok_extract_attempts_total", result="access_restricted")
            # 10216/10222 are private post/account permissions, not geo codes.
            # Rotating exits cannot grant account access.
            if VERBOSE_LOGS:
                print(f"  🔒 [Attempt {real_attempts}] ACCESS RESTRICTED on {proxy}: {e}", flush=True)
            raise HTTPException(status_code=403, detail=str(e))
        except TikTokIPBlockedError as e:
            inc_counter("tiktok_extract_attempts_total", result="ip_blocked")
            inc_counter("tiktok_proxy_failovers_total", reason="ip_blocked")
            # TikTok also returns 10204 for some rights/geo-restricted posts, so
            # this response alone cannot safely condemn a public exit IP. The
            # current request already excludes this proxy/IPv4 through
            # tried_proxies/tried_exit_ips. Only the neutral background block
            # probe may persist BLOCK and schedule a reconnect for the pool.
            last_error = e
            if VERBOSE_LOGS:
                print(f"  🚫 [Attempt {real_attempts}] ambiguous 10204 on {proxy}; "
                      "excluded for this request only", flush=True)
            if req_proxy:
                break
            control_verdict = await verify_ambiguous_ip_block(proxy)
            if control_verdict is True:
                blocked_proxies = mark_proxy_and_shared_exit_blocked(proxy)
                if VERBOSE_LOGS:
                    print(f"  🧱 [Attempt {real_attempts}] control post also blocked; "
                          f"parked {len(blocked_proxies)} proxy(s)", flush=True)
            elif control_verdict is False:
                if proxy_exit_ip:
                    failed_ip_block_exit_ips.add(proxy_exit_ip)
                    healthy_control_exit_ips.add(proxy_exit_ip)
                if VERBOSE_LOGS:
                    print(f"  ✅ [Attempt {real_attempts}] control post served; "
                          "10204 is content-specific", flush=True)
            elif proxy_exit_ip:
                # Still allow Indonesia as a recovery lane after two distinct
                # target failures, but do not later call the video itself bad
                # unless a neutral control request proved TikTok serves an exit.
                failed_ip_block_exit_ips.add(proxy_exit_ip)
            if (
                not prefer_indo
                and should_retry_ip_block_via_indonesia(len(failed_ip_block_exit_ips))
            ):
                # Some geo-restricted posts (including sports rights content)
                # return 10204 outside the permitted country. One 10204 is only
                # evidence of a bad exit; two distinct public IPv4s are enough
                # to try the configured Indonesia lane without misclassifying a
                # single WAF-blocked address as geo-restricted content.
                real_attempts -= 1
                retry_warp = False
                prefer_indo = True
                inc_counter("tiktok_proxy_failovers_total", reason="ip_block_to_indonesia")
                if VERBOSE_LOGS:
                    # Distinct countries, not just distinct IPv4s: two Singapore
                    # exits are two addresses but one place, so they are much
                    # weaker evidence that the post is geo-restricted.
                    countries = sorted(tried_countries) or ["unknown"]
                    print(f"  🇮🇩 [Attempt {real_attempts}] 10204 confirmed on "
                          f"{len(failed_ip_block_exit_ips)} distinct IPv4 exits "
                          f"across {','.join(countries)}; "
                          "switching to Indonesia nodes", flush=True)
            elif lane == "cloudflare":
                # A refused IP is a routing/infrastructure failure, not a content
                # attempt. Preserve the budget and try another unique WARP IPv4.
                real_attempts -= 1
                retry_warp = True
        except TikTokExtractError as e:
            inc_counter("tiktok_extract_attempts_total", result="extract_error")
            last_error = e
            if lane == "cloudflare":
                # Confirmation is based on TikTok's effective public IPv4. WARP
                # containers with different IPv6 addresses often share it, and
                # an unknown exit IP must never count as independent evidence.
                if proxy_exit_ip:
                    failed_warp_exit_ips.add(proxy_exit_ip)
                if should_retry_via_indonesia(lane, e, len(failed_warp_exit_ips)):
                    # Some region-locked posts return the same empty shell on
                    # multiple WARP exits instead of a 10216/10222 status. The
                    # second WARP request is a confirmation probe, so preserve
                    # the normal content-attempt budget for Indonesia failover.
                    real_attempts -= 1
                    prefer_indo = True
                else:
                    retry_warp = True
            if VERBOSE_LOGS:
                retry_lane = (
                    " -> switching to Indonesia nodes" if prefer_indo
                    else " -> confirming on another WARP exit" if retry_warp
                    else ""
                )
                print(f"  ⚠️ [Attempt {real_attempts}] CONTENT/EXTRACT FAIL on {proxy}{retry_lane}: {e}", flush=True)
            if req_proxy:
                break
        except Exception as e:
            inc_counter("tiktok_extract_attempts_total", result="unclassified")
            last_error = e
            if VERBOSE_LOGS:
                print(f"  ⚠️ [Attempt {real_attempts}] UNCLASSIFIED FAIL on {proxy}: {e}", flush=True)
            if req_proxy:
                break
        finally:
            dec_in_flight(proxy)

    print(f"❌ [FAILED] Extraction failed for {target_url} after {touches} proxy touches: {last_error}", flush=True)
    inc_counter("tiktok_extract_results_total", result="failed", source="none")
    if isinstance(last_error, TikTokIPBlockedError):
        if not healthy_control_exit_ips:
            raise HTTPException(
                status_code=503,
                detail="No healthy TikTok exit could verify this video; retry later",
            )
        if prefer_indo and indonesia_touches < 2:
            raise HTTPException(
                status_code=503,
                detail="Not enough usable Indonesia exits to verify this video",
            )
        if prefer_indo and indonesia_touches >= 2:
            raise HTTPException(
                status_code=422,
                detail=("TikTok video is unavailable or restricted after verification "
                        "on healthy Indonesia exits"),
            )
    raise HTTPException(status_code=502, detail=f"Extraction failed: {last_error}")


async def render_slideshow(
    photo_urls: list,
    audio_url: Optional[str],
    proxy: Optional[str],
    impersonate: str,
    duration_per_image: int = 3,
    cookies: Optional[str] = None,
) -> tuple[str, Path]:
    """Fetch the assets and mux the slideshow; returns (temp_dir, mp4_path).

    Rendering finishes before the response starts. It always did -- the old
    generator only opened the finished file, so nothing was ever streamed
    incrementally -- but doing it inside the generator meant a failed ffmpeg run
    reached the client as a 200 with an empty body, because the headers had
    already gone out. Raising here surfaces it as a real HTTP error instead.

    The caller owns temp_dir and must remove it once the file is served.
    """
    # Slideshows pull every photo plus the audio track through the proxy before
    # ffmpeg runs, so they load a node just like a video download does. The node
    # is released as soon as the render is done: serving the local file needs no
    # proxy traffic.
    if proxy:
        inc_in_flight(proxy)
    temp_dir = tempfile.mkdtemp(prefix="tiktok_slideshow_")
    work_dir = Path(temp_dir)
    proxies = {"http": proxy, "https": proxy} if proxy else None

    fetch_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.tiktok.com/",
    }
    if cookies:
        fetch_headers["Cookie"] = cookies

    try:
        async with AsyncSession(impersonate=impersonate, proxies=proxies) as session:
            image_paths = []
            for i, img_url in enumerate(photo_urls):
                img_path = work_dir / f"img_{i}.jpg"
                res = await session.get(img_url, headers=fetch_headers, timeout=15)
                # An error page written out as .jpg only fails later, inside
                # ffmpeg, where the cause is no longer visible.
                if res.status_code not in (200, 206):
                    raise HTTPException(
                        status_code=502,
                        detail=f"Slideshow photo {i + 1} unavailable (HTTP {res.status_code})")
                img_path.write_bytes(res.content)
                image_paths.append(str(img_path))

            audio_path = None
            if audio_url:
                aud_file = work_dir / "audio.mp3"
                res_aud = await session.get(audio_url, headers=fetch_headers, timeout=15)
                # Audio is optional: a silent slideshow beats no slideshow.
                if res_aud.status_code in (200, 206):
                    aud_file.write_bytes(res_aud.content)
                    audio_path = str(aud_file)

        list_path = work_dir / "images.txt"
        with open(list_path, "w", encoding="utf-8") as f:
            for p in image_paths:
                f.write(f"file '{p}'\nduration {duration_per_image}\n")
            f.write(f"file '{image_paths[-1]}'\n")

        out_mp4 = work_dir / "slideshow.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path)
        ]
        if audio_path:
            cmd.extend(["-stream_loop", "-1", "-i", audio_path])

        filter_parts = [
            "[0:v]scale=w=720:h=1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=24[vout]"
        ]
        if audio_path:
            total_duration = len(image_paths) * duration_per_image
            filter_parts.append(f"[1:a]atrim=0:{total_duration},asetpts=PTS-STARTPTS[aout]")
            filter_complex = ";".join(filter_parts)
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]"
            ])
        else:
            cmd.extend(["-filter_complex", filter_parts[0], "-map", "[vout]"])

        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p", str(out_mp4)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()

        if proc.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size == 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            print(f"[Slideshow] ffmpeg exited {proc.returncode} for {len(image_paths)} "
                  f"image(s): {detail[-500:]}", flush=True)
            inc_counter("tiktok_slideshow_render_total", result="failed")
            raise HTTPException(status_code=502, detail="Slideshow render failed")

        inc_counter("tiktok_slideshow_render_total", result="success")
        return temp_dir, out_mp4
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        if proxy:
            dec_in_flight(proxy)


async def stream_file_and_cleanup(temp_dir: str, path: Path):
    try:
        with open(path, "rb") as vf:
            while chunk := vf.read(64 * 1024):
                yield chunk
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.api_route("/tiktok/download", methods=["GET", "HEAD"])
@app.api_route("/download", methods=["GET", "HEAD"])
@app.api_route("/tunnel", methods=["GET", "HEAD"])
async def download_tiktok_media(
    request: Request,
    key: str = Query(..., description="Session key for download"),
    download: bool = Query(True, description="Download as attachment or stream inline"),
):
    session_data = get_session(key)
    if not session_data:
        raise HTTPException(status_code=404, detail="Download session expired or invalid")

    author = session_data.get("author") or "tiktok"
    media_type = session_data.get("type") or "video"
    inc_counter("tiktok_download_requests_total", media_type=media_type)
    proxy = session_data.get("proxy") or DEFAULT_PROXY or None
    impersonate = session_data.get("impersonate") or DEFAULT_IMPERSONATE
    session_cookies = session_data.get("cookies")
    disposition = "attachment" if download else "inline"

    # Slideshow video renderer
    if media_type in ("slideshow", "slideshow_render"):
        photo_urls = session_data.get("photo_urls") or []
        audio_url = session_data.get("audio_url")
        if not photo_urls:
            raise HTTPException(status_code=400, detail="No photos in slideshow session")
        filename = build_filename(session_data)
        safe_filename = quote(filename)
        temp_dir, out_mp4 = await render_slideshow(
            photo_urls, audio_url, proxy, impersonate, cookies=session_cookies)
        return StreamingResponse(
            stream_file_and_cleanup(temp_dir, out_mp4),
            media_type="video/mp4",
            headers={
                "Content-Type": "video/mp4",
                # The file is complete before the response starts, so the length
                # is known and the client gets a real progress bar.
                "Content-Length": str(out_mp4.stat().st_size),
                "Content-Disposition": f"{disposition}; filename=\"{filename}\"; filename*=UTF-8''{safe_filename}",
            },
        )

    direct_url = session_data.get("direct_url")
    if not direct_url:
        raise HTTPException(status_code=400, detail="Session does not contain direct_url")

    content_type = "video/mp4"
    if media_type == "mp3":
        content_type = "audio/mpeg"
    elif media_type == "photo":
        content_type = "image/jpeg"

    filename = build_filename(session_data)
    safe_filename = quote(filename)

    upstream_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        # yt-dlp sends the video's own page as Referer (http_headers={'Referer': webpage_url}),
        # not a generic homepage. Match that: the CDN accepts both today, but the
        # per-video Referer is what a real player sends.
        "Referer": session_data.get("url") or "https://www.tiktok.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if session_cookies:
        upstream_headers["Cookie"] = session_cookies

    range_header = request.headers.get("Range")
    if range_header and media_type != "mp3":
        upstream_headers["Range"] = range_header
    elif media_type == "video":
        upstream_headers["Range"] = "bytes=0-"

    resp = None
    client = None
    streaming_proxy = None
    tried_proxies: set[str] = set()
    tried_exit_ips: set[str] = set()
    candidate_proxy = proxy
    direct_attempted = False

    for _attempt in range(DOWNLOAD_PROXY_ATTEMPTS):
        if not candidate_proxy:
            if not ALLOW_DIRECT_DOWNLOAD_FALLBACK or direct_attempted:
                break
            direct_attempted = True
        if candidate_proxy:
            tried_proxies.add(candidate_proxy)
            if exit_ip := get_proxy_exit_ip(candidate_proxy):
                tried_exit_ips.add(exit_ip)
            inc_in_flight(candidate_proxy)
        proxy_dict = {"http": candidate_proxy, "https": candidate_proxy} if candidate_proxy else None
        transport_failed = False
        try:
            client = AsyncSession(impersonate=impersonate, proxies=proxy_dict)
            resp = await client.get(direct_url, headers=upstream_headers, stream=True, allow_redirects=True, timeout=30)
            if resp.status_code in (200, 206):
                streaming_proxy = candidate_proxy
                break
            await client.close()
            client = None
        except Exception as exc:
            transport_failed = should_cooldown_download_failure(exc)
            if client:
                await client.close()
                client = None
        if candidate_proxy:
            dec_in_flight(candidate_proxy)
            if transport_failed:
                mark_proxy_cooldown(candidate_proxy, 30)
        candidate_proxy = get_next_proxy(
            exclude=tried_proxies,
            exclude_exit_ips=tried_exit_ips,
        )
        if not candidate_proxy:
            if ALLOW_DIRECT_DOWNLOAD_FALLBACK and not direct_attempted:
                candidate_proxy = None
            else:
                break

    if not resp or resp.status_code not in (200, 206):
        if client:
            await client.close()
        status_err = resp.status_code if resp else "timeout"
        raise HTTPException(status_code=502, detail=f"CDN streaming error: HTTP {status_err}")

    response_headers = {
        "Content-Type": content_type if media_type == "mp3" else resp.headers.get("Content-Type", content_type),
        "Content-Disposition": f"{disposition}; filename=\"{filename}\"; filename*=UTF-8''{safe_filename}",
    }
    if media_type != "mp3":
        response_headers["Accept-Ranges"] = "bytes"
    if media_type != "mp3" and "Content-Length" in resp.headers:
        response_headers["Content-Length"] = resp.headers["Content-Length"]
    if media_type != "mp3" and "Content-Range" in resp.headers:
        response_headers["Content-Range"] = resp.headers["Content-Range"]

    if request.method == "HEAD":
        if streaming_proxy:
            dec_in_flight(streaming_proxy)
        await client.close()
        return Response(status_code=200 if media_type == "mp3" else resp.status_code, headers=response_headers)

    async def stream_transcoded_mp3():
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k",
            "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def feed_input():
            try:
                async for chunk in resp.aiter_content(chunk_size=64 * 1024):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            finally:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()

        feeder = asyncio.create_task(feed_input())
        try:
            while chunk := await proc.stdout.read(64 * 1024):
                yield chunk
            await feeder
            await proc.wait()
        finally:
            if not feeder.done():
                feeder.cancel()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if streaming_proxy:
                dec_in_flight(streaming_proxy)
            await client.close()

    async def stream_generator():
        try:
            async for chunk in resp.aiter_content(chunk_size=64 * 1024):
                yield chunk
        finally:
            if streaming_proxy:
                dec_in_flight(streaming_proxy)
            if client:
                await client.close()

    return StreamingResponse(
        stream_transcoded_mp3() if media_type == "mp3" else stream_generator(),
        status_code=200 if media_type == "mp3" else resp.status_code,
        headers=response_headers,
    )


def get_auto_workers() -> int:
    env_workers = os.getenv("WORKERS") or os.getenv("GRANIAN_WORKERS")
    if env_workers:
        try:
            return int(env_workers)
        except ValueError:
            pass
    if hasattr(os, "sched_getaffinity"):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except Exception:
            pass
    return max(1, os.cpu_count() or 2)


if __name__ == "__main__":
    from granian import Granian
    from granian.constants import Interfaces

    auto_workers = get_auto_workers()
    print(f"🚀 Starting Granian on {HOST}:{PORT} with {auto_workers} auto-detected CPU workers")
    server = Granian(
        "main:app",
        address=HOST,
        port=PORT,
        interface=Interfaces.ASGI,
        workers=auto_workers,
        runtime_threads=2,
    )
    server.serve()
