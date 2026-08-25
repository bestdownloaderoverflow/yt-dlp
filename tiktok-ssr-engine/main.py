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
    get_next_proxy, get_proxy_pool, get_warp_proxies,
)
from extractor import (
    TikTokExtractError,
    TikTokGeoBlockedError,
    TikTokInfraError,
    TikTokRegionRestrictedError,
    TikTokSSRExtractor,
    build_filename,
)
from proxy_state import (
    dec_in_flight,
    get_proxy_exit_ip,
    inc_in_flight,
    mark_proxy_blocked,
    mark_proxy_cooldown,
    mark_proxy_request_success,
)
from session import get_cached_extraction, get_session, set_cached_extraction


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


class TikTokRequest(BaseModel):
    url: str
    proxy: Optional[str] = None
    impersonate: Optional[str] = None


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
    return Response(
        content=f"# HELP tiktok_ssr_uptime_seconds Uptime in seconds\ntiktok_ssr_uptime_seconds {int(time.time() - START_TIME)}\n",
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
        if VERBOSE_LOGS:
            print(f"⚡ [CACHE HIT] Returning cached result for {target_url}", flush=True)
        return JSONResponse(content=cached)

    impersonate = req_impersonate or DEFAULT_IMPERSONATE

    max_attempts = MAX_ATTEMPTS if not req_proxy else 1
    max_touches = 8
    real_attempts = 0
    touches = 0
    prefer_indo = False
    last_error = None
    tried_proxies: set[str] = set()
    tried_exit_ips: set[str] = set()

    if VERBOSE_LOGS:
        print(f"📥 [REQUEST] Extracting: {target_url}", flush=True)

    while real_attempts < max_attempts and touches < max_touches:
        touches += 1
        real_attempts += 1

        if prefer_indo:
            # Region-locked content still overrides everything: only Indonesia exits
            # can see it, so Cloudflare-first does not apply once we know that.
            proxy = req_proxy or get_next_proxy(
                indo_only=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
            ) or get_next_proxy(
                prefer_geo=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
            )
            lane = "indo"
        elif real_attempts == 1:
            # First attempt goes through Cloudflare WARP, falling back to the geo
            # pool only when every WARP node is dead, cooling down or blocked.
            proxy = req_proxy or get_next_proxy(
                warp_only=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
            )
            lane = "cloudflare"
            if not proxy:
                proxy = get_next_proxy(
                    prefer_geo=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
                )
                lane = "geo (no WARP available)"
        else:
            proxy = req_proxy or get_next_proxy(
                prefer_geo=True, exclude=tried_proxies, exclude_exit_ips=tried_exit_ips,
            )
            lane = "geo"
        proxy = proxy or DEFAULT_PROXY or None
        if PROXY_COUNT > 0 and not proxy:
            last_error = TikTokInfraError("No usable proxy exits available")
            break
        if proxy:
            tried_proxies.add(proxy)
            if exit_ip := get_proxy_exit_ip(proxy):
                tried_exit_ips.add(exit_ip)

        if VERBOSE_LOGS:
            print(f"  🔄 [Attempt {real_attempts}/{max_attempts}] lane={lane} proxy={proxy}", flush=True)
        inc_in_flight(proxy)
        try:
            extractor = TikTokSSRExtractor(proxy=proxy, impersonate=impersonate)
            result = await extractor.extract(target_url)
            source = result.get("extract_source") or ""
            mark_proxy_request_success(proxy, tiktok_served=source != "web_fallback")
            if VERBOSE_LOGS:
                print(f"  ✅ [Attempt {real_attempts}] SUCCESS ({result.get('extract_source')}) - Title: {result.get('title', '')[:40]}", flush=True)

            # Save successful extraction to cache
            set_cached_extraction(cache_key, result, ttl=CACHE_TTL or 300)
            return JSONResponse(content=result)
        except TikTokInfraError as e:
            # Proxy infrastructure failure: switch proxy WITHOUT consuming an attempt
            real_attempts -= 1
            last_error = e
            mark_proxy_cooldown(proxy, 60)
            if VERBOSE_LOGS:
                print(f"  ⚠️ [Touch {touches}] INFRA FAIL on {proxy} (attempt not consumed, cooldown 60s): {e}", flush=True)
            if req_proxy:
                break
        except TikTokRegionRestrictedError as e:
            # Region-restricted content: go straight to Indonesia nodes
            last_error = e
            mark_proxy_cooldown(proxy, 30)
            if VERBOSE_LOGS:
                print(f"  ⚠️ [Attempt {real_attempts}] REGION-RESTRICTED on {proxy} (cooldown 30s) -> switching to Indonesia nodes", flush=True)
            if req_proxy:
                break
            prefer_indo = True
        except TikTokGeoBlockedError as e:
            # WAF verdict on the exit IP itself: park it as blocked so the whole
            # pool stops picking it, not just this request.
            last_error = e
            mark_proxy_blocked(proxy)
            if VERBOSE_LOGS:
                print(f"  🚫 [Attempt {real_attempts}] IP BLOCKED on {proxy} -> marked blocked, switching to Indonesia nodes", flush=True)
            if req_proxy:
                break
            prefer_indo = True
        except TikTokExtractError as e:
            last_error = e
            mark_proxy_cooldown(proxy, 15)
            if VERBOSE_LOGS:
                print(f"  ⚠️ [Attempt {real_attempts}] FAILED on {proxy} (cooldown 15s): {e}", flush=True)
            if req_proxy:
                break
        except Exception as e:
            last_error = e
            mark_proxy_cooldown(proxy, 15)
            if VERBOSE_LOGS:
                print(f"  ⚠️ [Attempt {real_attempts}] FAILED on {proxy} (cooldown 15s): {e}", flush=True)
            if req_proxy:
                break
        finally:
            dec_in_flight(proxy)

    print(f"❌ [FAILED] Extraction failed for {target_url} after {touches} proxy touches: {last_error}", flush=True)
    raise HTTPException(status_code=502, detail=f"Extraction failed: {last_error}")


async def stream_rendered_slideshow(
    photo_urls: list,
    audio_url: Optional[str],
    proxy: Optional[str],
    impersonate: str,
    duration_per_image: int = 3,
    cookies: Optional[str] = None,
):
    # Slideshows pull every photo plus the audio track through the proxy before
    # ffmpeg runs, so they load a node just like a video download does.
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
                img_path.write_bytes(res.content)
                image_paths.append(str(img_path))

            audio_path = None
            if audio_url:
                aud_file = work_dir / "audio.mp3"
                res_aud = await session.get(audio_url, headers=fetch_headers, timeout=15)
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

        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.communicate()

        if out_mp4.exists():
            with open(out_mp4, "rb") as vf:
                while chunk := vf.read(64 * 1024):
                    yield chunk
    finally:
        if proxy:
            dec_in_flight(proxy)
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
        return StreamingResponse(
            stream_rendered_slideshow(photo_urls, audio_url, proxy, impersonate, cookies=session_cookies),
            media_type="video/mp4",
            headers={
                "Content-Type": "video/mp4",
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
        try:
            client = AsyncSession(impersonate=impersonate, proxies=proxy_dict)
            resp = await client.get(direct_url, headers=upstream_headers, stream=True, allow_redirects=True, timeout=30)
            if resp.status_code in (200, 206):
                streaming_proxy = candidate_proxy
                break
            await client.close()
            client = None
        except Exception:
            if client:
                await client.close()
                client = None
        if candidate_proxy:
            dec_in_flight(candidate_proxy)
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
