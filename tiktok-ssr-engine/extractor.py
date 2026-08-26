import asyncio
import base64
import hashlib
import json
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from config import DEFAULT_IMPERSONATE, load_cookie_string
from session import create_session


def generate_blockbuster_headers(min_headers: int = 2, max_headers: int = 6) -> Dict[str, str]:
    def random_letters(minimum: int, maximum: int) -> str:
        return "".join(random.choices("bcdfghjklmnpqrstvwxz", k=random.randint(minimum, maximum)))

    return {
        random_letters(8, 20): random_letters(16, 28)
        for _ in range(random.randint(min_headers, max_headers))
    }


class TikTokExtractError(Exception):
    """Base class for TikTok extraction errors with retry semantics."""

    retryable = True
    message = "TikTok extraction failed"

    def __init__(self, msg: str = ""):
        super().__init__(msg or self.message)


class TikTokIPBlockedError(TikTokExtractError):
    """IP blocked / WAF challenge (status 10204). Retry with another exit IP."""

    message = "TikTok IP blocked / WAF challenge (code 10204)"


class TikTokAccessRestrictedError(TikTokExtractError):
    """Private post/account or classified content that requires account access."""

    retryable = False
    message = "TikTok content is private or requires an account with access"


class TikTokInfraError(TikTokExtractError):
    """Proxy infrastructure failure (DNS resolve, connect refused, timeout)."""

    message = "Proxy infrastructure failure"


def _classify_error(e: Exception) -> TikTokExtractError:
    if isinstance(e, TikTokExtractError):
        return e
    code = getattr(e, "code", None)
    msg = str(e).lower()
    if code in (5, 7, 28, 97) or any(
        s in msg for s in (
            "could not resolve", "connection refused", "timed out", "resolve proxy",
            "cannot complete socks5 connection",
        )
    ):
        return TikTokInfraError(str(e))
    return TikTokExtractError(str(e))


DESKTOP_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def sanitize_filename_part(value: Optional[str], fallback: str = "tiktok") -> str:
    if not value:
        return fallback
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return cleaned or fallback


def build_filename(session_data: Dict[str, Any]) -> str:
    author = session_data.get("author") or "tiktok"
    media_type = session_data.get("type") or "video"
    if media_type == "photo":
        idx = session_data.get("photo_index") or session_data.get("index") or 1
        return f"{author}_photo_{idx}.jpeg"
    if media_type == "mp3":
        return f"{author}_mp3.mp3"
    if media_type in ("slideshow", "slideshow_render"):
        return f"{author}_slideshow.mp4"
    quality = session_data.get("quality") or ""
    if quality:
        return f"{author}_video_{quality}.mp4"
    return f"{author}_video.mp4"


def _first_url(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list) and len(val) > 0:
        item = val[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("url", "") or item.get("urlList", [""])[0] or item.get("url_list", [""])[0]
    if isinstance(val, dict):
        return val.get("url", "") or val.get("UrlList", [""])[0] or val.get("urlList", [""])[0] or val.get("url_list", [""])[0]
    return ""


def _media_identity(value: Any) -> str:
    """Best-effort identity used to avoid advertising CDN mirrors as qualities."""
    if isinstance(value, dict):
        key = value.get("UrlKey") or value.get("urlKey") or value.get("url_key")
        if key:
            return f"key:{key}"
    url = _first_url(value)
    if not url:
        return ""
    parsed = urlparse(url)
    # TikTok frequently serves the same object from v16/v19 mirror hosts.
    return f"path:{parsed.path}"


def _decode_ssstik_url(url: str) -> str:
    if not url:
        return ""
    if "tikcdn.io/ssstik/" in url:
        part = url.split("tikcdn.io/ssstik/")[-1].lstrip("m/").lstrip("s/")
        part += "=" * ((4 - len(part) % 4) % 4)
        try:
            decoded = base64.b64decode(part).decode("utf-8")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
    return url


def solve_slardar_challenge(html: str) -> Optional[Dict[str, str]]:
    cs_match = re.search(r'id=["\']cs["\']\s+class=["\']([^"\']+)["\']', html)
    wci_match = re.search(r'id=["\']wci["\']\s+class=["\']([^"\']+)["\']', html)
    if not cs_match or not wci_match:
        return None

    cs_raw = cs_match.group(1)
    cs_raw += "=" * ((4 - len(cs_raw) % 4) % 4)
    try:
        challenge_data = json.loads(base64.b64decode(cs_raw).decode("utf-8"))
        expected_digest = base64.b64decode(challenge_data["v"]["c"])
        base_hash = hashlib.sha256(base64.b64decode(challenge_data["v"]["a"]))

        for i in range(1_000_001):
            num_bytes = str(i).encode("utf-8")
            h = base_hash.copy()
            h.update(num_bytes)
            if h.digest() == expected_digest:
                challenge_data["d"] = base64.b64encode(num_bytes).decode("utf-8")
                break
        else:
            return None

        wci_cookie_name = wci_match.group(1)
        wci_cookie_value = base64.b64encode(
            json.dumps(challenge_data, separators=(",", ":")).encode("utf-8")
        ).decode("utf-8")

        cookies = {wci_cookie_name: wci_cookie_value}
        rci_match = re.search(r'id=["\']rci["\']\s+class=["\']([^"\']+)["\']', html)
        rs_match = re.search(r'id=["\']rs["\']\s+class=["\']([^"\']+)["\']', html)
        if rci_match and rs_match:
            cookies[rci_match.group(1)] = rs_match.group(1)
        return cookies
    except Exception:
        return None


def _session_cookie_string(session: AsyncSession) -> str:
    try:
        if not session or not session.cookies:
            return ""
        cookie_dict = dict(session.cookies)
        if cookie_dict:
            return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    except Exception:
        pass
    return ""


class TikTokSSRExtractor:
    def __init__(self, proxy: Optional[str] = None, impersonate: Optional[str] = None):
        self.proxy = proxy
        self.impersonate = impersonate or DEFAULT_IMPERSONATE

    async def resolve_url(self, session: AsyncSession, url: str) -> str:
        clean_url = url.split("?")[0].strip()
        parsed = urlparse(clean_url)
        if any(h in parsed.netloc for h in ["vm.tiktok.com", "vt.tiktok.com", "t.tiktok.com", "m.tiktok.com"]):
            headers = dict(DESKTOP_BROWSER_HEADERS)
            headers["Referer"] = "https://www.tiktok.com/"
            resp = await session.get(url, headers=headers, allow_redirects=True, timeout=10)
            return resp.url.split("?")[0].strip()
        return clean_url

    async def extract_via_web_ssr(self, session: AsyncSession, canonical_url: str) -> Optional[Dict[str, Any]]:
        cookie_str = load_cookie_string()
        headers = dict(DESKTOP_BROWSER_HEADERS)
        if cookie_str:
            headers["Cookie"] = cookie_str
        headers["Referer"] = "https://www.tiktok.com/"
        headers.update(generate_blockbuster_headers())

        resp = await session.get(canonical_url, headers=headers, timeout=15)
        html = resp.text

        # Solve challenge if present
        if "SlardarWAF" in html or "_wafchallengeid" in html:
            # CPU-bound (up to 1M SHA256 iterations): run in a thread so the
            # event loop keeps serving other requests during the solve.
            solved = await asyncio.to_thread(solve_slardar_challenge, html)
            if solved:
                for k, v in solved.items():
                    session.cookies.set(k, v, domain=".tiktok.com")
                headers.pop("Cookie", None)
                resp = await session.get(canonical_url, headers=headers, timeout=15)
                html = resp.text

        session_cookies = _session_cookie_string(session)

        # 1. Primary: __UNIVERSAL_DATA_FOR_REHYDRATION__
        match = re.search(
            r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"\s+type="application/json">([^<]+)</script>',
            html,
        )
        if match:
            try:
                data = json.loads(match.group(1))
                scope = data.get("__DEFAULT_SCOPE__", {})
                detail = scope.get("webapp.video-detail", {})
                
                # Status code classification
                status_code = detail.get("statusCode")
                if status_code == 10204:
                    raise TikTokIPBlockedError()
                elif status_code in (10216, 10222):
                    restriction = "private post" if status_code == 10216 else "private account"
                    raise TikTokAccessRestrictedError(
                        f"TikTok {restriction}; log into an account that has access (code {status_code})")
                
                item = detail.get("itemInfo", {}).get("itemStruct")
                if item:
                    if item.get("isContentClassified") and not item.get("video") and not item.get("imagePost"):
                        raise TikTokAccessRestrictedError("TikTok content is age-classified / login required")
                    return self.build_response_from_ssr(item, canonical_url, source="web_ssr", cookies=session_cookies)
            except TikTokExtractError:
                raise
            except Exception:
                pass

        # 2. Secondary: SIGI_STATE
        match_sigi = re.search(
            r'<script\s+id="SIGI_STATE"\s+type="application/json">([^<]+)</script>',
            html,
        )
        if match_sigi:
            try:
                data = json.loads(match_sigi.group(1))
                items = data.get("ItemModule", {})
                if items:
                    item = next(iter(items.values()))
                    return self.build_response_from_ssr(item, canonical_url, source="web_sigi", cookies=session_cookies)
            except Exception:
                pass

        # 3. Tertiary: __NEXT_DATA__
        match_next = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json">([^<]+)</script>',
            html,
        )
        if match_next:
            try:
                data = json.loads(match_next.group(1))
                props = data.get("props", {}).get("pageProps", {})
                item = props.get("itemInfo", {}).get("itemStruct")
                if item:
                    return self.build_response_from_ssr(item, canonical_url, source="web_next", cookies=session_cookies)
            except Exception:
                pass

        return None

    async def extract_via_embed_ssr(self, session: AsyncSession, item_id: str, canonical_url: str) -> Optional[Dict[str, Any]]:
        headers = dict(DESKTOP_BROWSER_HEADERS)
        headers["Referer"] = "https://www.tiktok.com/"
        embed_url = f"https://www.tiktok.com/embed/v2/{item_id}"
        resp = await session.get(embed_url, headers=headers, timeout=12)
        match = re.search(r'<script\s+id="__FRONTITY_CONNECT_STATE__"\s+type="application/json">([^<]+)</script>', resp.text)
        if not match:
            return None

        session_cookies = _session_cookie_string(session)

        try:
            d = json.loads(match.group(1))
            source = d.get("source", {})
            data = source.get("data", {})
            for k, v in data.items():
                if isinstance(v, dict) and "videoData" in v:
                    vdata = v["videoData"]
                    return self.build_response_from_frontity(vdata, canonical_url, source="web_embed_ssr", cookies=session_cookies)
        except Exception:
            pass
        return None

    async def extract_via_web_fallback(self, session: AsyncSession, canonical_url: str) -> Optional[Dict[str, Any]]:
        home_resp = await session.get("https://ssstik.io/en", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        tt_match = re.search(r's_tt\s*=\s*["\']([^"\']+)["\']', home_resp.text)
        if not tt_match:
            return None
        tt_val = tt_match.group(1)

        post_data = {"id": canonical_url, "locale": "en", "tt": tt_val}
        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://ssstik.io",
            "Referer": "https://ssstik.io/en",
            "User-Agent": "Mozilla/5.0",
        }
        res = await session.post("https://ssstik.io/abc?url=dl", data=post_data, headers=post_headers, timeout=15)
        html = res.text

        author_match = re.search(r'<h2[^>]*>([^<]+)</h2>', html)
        title_match = re.search(r'<p[^>]*class=["\']maintext["\'][^>]*>([^<]+)</p>', html)
        nickname = author_match.group(1).strip() if author_match else "tiktok_user"
        title = title_match.group(1).strip() if title_match else ""
        safe_author = sanitize_filename_part(nickname)

        session_cookies = _session_cookie_string(session)

        # Check Photo / Slide elements
        slide_matches = re.findall(r'href=["\'](https://tikcdn\.io/ssstik/[^"\'m/][^"\']*)["\']', html) or re.findall(r'data-splide-lazy=["\'](https://[^"\']+)["\']', html)
        if slide_matches:
            seen = set()
            image_urls = []
            for m in slide_matches:
                decoded = _decode_ssstik_url(m)
                if decoded and decoded not in seen:
                    seen.add(decoded)
                    image_urls.append(decoded)

            # Audio
            music_match = re.search(r'href=["\'](https://tikcdn\.io/ssstik/m/[^"\']+)["\']', html) or re.search(r'href=["\'](https://[^"\']+)["\'][^>]*class=["\'][^"\']*music["\']', html)
            audio_url = _decode_ssstik_url(music_match.group(1)) if music_match else ""

            photo_keys = []
            photos = []
            for i, img_url in enumerate(image_urls):
                k = create_session({
                    "url": canonical_url,
                    "type": "photo",
                    "photo_index": i + 1,
                    "direct_url": img_url,
                    "author": safe_author,
                    "proxy": self.proxy,
                    "impersonate": self.impersonate,
                    "cookies": session_cookies,
                })
                link = f"/tiktok/download?key={k}"
                photo_keys.append(link)
                photos.append({"type": "photo", "url": img_url, "download_link": link})

            download_link: Dict[str, Any] = {"no_watermark": photo_keys}
            if audio_url:
                mp3_key = create_session({
                    "url": canonical_url,
                    "type": "mp3",
                    "direct_url": audio_url,
                    "author": safe_author,
                    "proxy": self.proxy,
                    "impersonate": self.impersonate,
                    "cookies": session_cookies,
                })
                download_link["mp3"] = f"/tiktok/download?key={mp3_key}"

            slideshow_key = create_session({
                "url": canonical_url,
                "type": "slideshow",
                "photo_urls": image_urls,
                "audio_url": audio_url,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "cookies": session_cookies,
            })

            return {
                "status": "picker",
                "extract_source": "web_fallback",
                "title": title,
                "description": title,
                "statistics": {"play_count": 0, "digg_count": 0, "comment_count": 0, "share_count": 0},
                "artist": nickname,
                "cover": image_urls[0] if image_urls else "",
                "duration": len(image_urls) * 3,
                "audio": audio_url,
                "download_link": download_link,
                "photos": photos,
                "download_slideshow": f"/tiktok/download?key={slideshow_key}",
                "download_slideshow_link": f"/tiktok/download?key={slideshow_key}",
                "author": {
                    "nickname": nickname,
                    "uniqueId": nickname,
                    "signature": "",
                    "avatar": "",
                    "avatarThumb": "",
                    "avatarMedium": "",
                    "avatarLarger": "",
                },
            }

        # Video elements
        no_wm_match = re.search(r'href=["\'](https://[^"\']+)["\'][^>]*class=["\'][^"\']*without_watermark', html) or re.search(r'class=["\'][^"\']*without_watermark[^"\']*["\'][^>]*href=["\'](https://[^"\']+)["\']', html)
        music_match = re.search(r'href=["\'](https://[^"\']+)["\'][^>]*class=["\'][^"\']*music["\']', html)

        no_wm_url = no_wm_match.group(1) if no_wm_match else ""
        music_url = music_match.group(1) if music_match else ""

        if not no_wm_url:
            return None

        key_sd = create_session({
            "url": canonical_url,
            "type": "video",
            "quality": "no_watermark",
            "direct_url": no_wm_url,
            "author": safe_author,
            "proxy": self.proxy,
            "impersonate": self.impersonate,
            "cookies": session_cookies,
        })

        download_link = {
            "no_watermark": f"/tiktok/download?key={key_sd}",
        }

        if music_url:
            key_mp3 = create_session({
                "url": canonical_url,
                "type": "mp3",
                "direct_url": music_url,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "cookies": session_cookies,
            })
            download_link["mp3"] = f"/tiktok/download?key={key_mp3}"

        return {
            "status": "tunnel",
            "extract_source": "web_fallback",
            "title": title,
            "description": title,
            "statistics": {"play_count": 0, "digg_count": 0, "comment_count": 0, "share_count": 0},
            "artist": nickname,
            "cover": "",
            "duration": 0,
            "audio": music_url,
            "download_link": download_link,
            "author": {
                "nickname": nickname,
                "uniqueId": nickname,
                "signature": "",
                "avatar": "",
                "avatarThumb": "",
                "avatarMedium": "",
                "avatarLarger": "",
            },
        }

    async def extract(self, url: str) -> Dict[str, Any]:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

        async with AsyncSession(impersonate=self.impersonate, proxies=proxies) as session:
            final_url = await self.resolve_url(session, url)

            classified_error: Optional[TikTokExtractError] = None

            # Strategy 1: Direct Web SSR Scraping
            try:
                res = await self.extract_via_web_ssr(session, final_url)
                if res:
                    return res
            except Exception as e:
                classified_error = _classify_error(e)
                print(f"[SSR Engine] Web SSR failed on proxy {self.proxy}: {e}")
                if isinstance(classified_error, TikTokInfraError):
                    raise classified_error

            # Strategy 2: Official Embed SSR Scraping (__FRONTITY_CONNECT_STATE__)
            item_id_match = re.search(r'/(?:video|photo)/(\d+)', final_url)
            if item_id_match:
                item_id = item_id_match.group(1)
                try:
                    res = await self.extract_via_embed_ssr(session, item_id, final_url)
                    if res:
                        return res
                except Exception as e:
                    classified_error = _classify_error(e)
                    print(f"[SSR Engine] Embed SSR failed on proxy {self.proxy}: {e}")
                    if isinstance(classified_error, TikTokInfraError):
                        raise classified_error

            # Strategy 3: Web Scraper Fallback Engine
            try:
                res = await self.extract_via_web_fallback(session, final_url)
                if res:
                    return res
            except Exception as e:
                classified_error = _classify_error(e)
                print(f"[SSR Engine] Web Fallback failed on proxy {self.proxy}: {e}")
                if isinstance(classified_error, TikTokInfraError):
                    raise classified_error

            if classified_error is not None:
                raise classified_error
            raise TikTokExtractError("Unable to extract video or photo slideshow from TikTok. Verify the post is public.")

    def build_response_from_ssr(self, item: Dict[str, Any], canonical_url: str, source: str = "web_ssr", cookies: str = "") -> Dict[str, Any]:
        author_data = item.get("author", {})
        nickname = author_data.get("nickname") or author_data.get("uniqueId") or "unknown"
        unique_id = author_data.get("uniqueId") or nickname
        avatar = author_data.get("avatarLarger") or author_data.get("avatarMedium") or author_data.get("avatarThumb") or ""
        safe_author = sanitize_filename_part(nickname)

        stats = item.get("stats") or item.get("statistics") or {}
        statistics = {
            "play_count": int(stats.get("playCount") or 0),
            "digg_count": int(stats.get("diggCount") or 0),
            "comment_count": int(stats.get("commentCount") or 0),
            "share_count": int(stats.get("shareCount") or 0),
        }

        title = item.get("desc") or ""
        description = item.get("desc") or ""
        music_data = item.get("music", {})
        audio_url = music_data.get("playUrl") or ""
        music_duration = int(music_data.get("duration") or 0)

        # Check Photo / Slideshow
        image_post = item.get("imagePost") or item.get("images")
        images = []
        if isinstance(image_post, dict) and "images" in image_post:
            images = image_post["images"]
        elif isinstance(image_post, list):
            images = image_post
        image_urls = [
            _first_url(img.get("imageURL") if isinstance(img, dict) else img)
            for img in images
        ]
        image_urls = [u for u in image_urls if u]

        if image_post and image_urls:
            photo_keys = []
            photos = []
            for i, img_url in enumerate(image_urls):
                k = create_session({
                    "url": canonical_url,
                    "type": "photo",
                    "photo_index": i + 1,
                    "direct_url": img_url,
                    "author": safe_author,
                    "proxy": self.proxy,
                    "impersonate": self.impersonate,
                    "cookies": cookies,
                })
                link = f"/tiktok/download?key={k}"
                photo_keys.append(link)
                photos.append({"type": "photo", "url": img_url, "download_link": link})

            download_link: Dict[str, Any] = {"no_watermark": photo_keys}
            if audio_url:
                mp3_key = create_session({
                    "url": canonical_url,
                    "type": "mp3",
                    "direct_url": audio_url,
                    "author": safe_author,
                    "proxy": self.proxy,
                    "impersonate": self.impersonate,
                    "cookies": cookies,
                })
                download_link["mp3"] = f"/tiktok/download?key={mp3_key}"

            slideshow_key = create_session({
                "url": canonical_url,
                "type": "slideshow",
                "photo_urls": image_urls,
                "audio_url": audio_url,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "cookies": cookies,
            })

            return {
                "status": "picker",
                "extract_source": source,
                "title": title,
                "description": description,
                "statistics": statistics,
                "artist": nickname,
                "cover": image_urls[0] if image_urls else "",
                "duration": len(image_urls) * 3,
                "audio": audio_url,
                "download_link": download_link,
                "photos": photos,
                "download_slideshow": f"/tiktok/download?key={slideshow_key}",
                "download_slideshow_link": f"/tiktok/download?key={slideshow_key}",
                "author": {
                    "nickname": nickname,
                    "uniqueId": unique_id,
                    "signature": author_data.get("signature") or "",
                    "avatar": avatar,
                    "avatarThumb": avatar,
                    "avatarMedium": avatar,
                    "avatarLarger": avatar,
                },
            }

        # Video Post
        video_data = item.get("video", {})
        play_addr_data = video_data.get("playAddr")
        download_addr_data = video_data.get("downloadAddr")
        play_addr = _first_url(play_addr_data)
        download_addr = _first_url(download_addr_data)
        cover = _first_url(video_data.get("cover")) or _first_url(video_data.get("originCover"))
        dynamic_cover = _first_url(video_data.get("dynamicCover"))
        duration = int(video_data.get("duration") or 0)

        # Codec detection & Selection
        # Prioritize H.264 / AVC1 for 100% universal playback compatibility
        # Filter out bytevc2 (unplayable) and /media-video-hvc1/ (broken 404 stream)
        hd_play_addr = ""
        hd_play_identity = ""
        bitrate_info = video_data.get("bitrateInfo") or []
        if bitrate_info and isinstance(bitrate_info, list):
            valid_bitrates = []
            for b in bitrate_info:
                if not isinstance(b, dict):
                    continue
                p_addr = _first_url(b.get("PlayAddr") or b.get("playAddr"))
                if not p_addr:
                    continue
                if "/media-video-hvc1/" in p_addr:
                    continue
                gear = str(b.get("GearName") or "").lower()
                codec_type = str(b.get("CodecType") or "").lower()
                url_key = str(b.get("PlayAddr", {}).get("UrlKey") or "")
                
                # Exclude bytevc2 (unplayable custom codec)
                if "bytevc2" in gear or "bytevc2" in codec_type or "bytevc2" in url_key:
                    continue
                
                # Priority: H.264/AVC1 is preferred over bytevc1 (H.265)
                is_h264 = "h264" in gear or "h264" in codec_type or "h264" in url_key or "avc1" in codec_type
                bitrate_val = int(b.get("Bitrate", 0) or b.get("bitrate", 0) or 0)
                
                valid_bitrates.append({
                    "url": p_addr,
                    "identity": _media_identity(b.get("PlayAddr") or b.get("playAddr")),
                    "bitrate": bitrate_val,
                    "is_h264": is_h264,
                })
            
            if valid_bitrates:
                valid_bitrates.sort(key=lambda x: (x["is_h264"], x["bitrate"]), reverse=True)
                hd_play_addr = valid_bitrates[0]["url"]
                hd_play_identity = valid_bitrates[0]["identity"]

        # Subtitles / Captions
        subtitles = {}
        subtitle_infos = video_data.get("subtitleInfos") or item.get("subtitleInfos") or []
        if isinstance(subtitle_infos, list):
            for sub in subtitle_infos:
                if isinstance(sub, dict):
                    lang = sub.get("LanguageCodeName") or sub.get("Language") or "en"
                    sub_url = sub.get("Url") or sub.get("url") or ""
                    if sub_url:
                        subtitles.setdefault(lang, []).append({
                            "url": sub_url,
                            "lang": sub.get("LanguageName") or lang,
                            "ext": "vtt" if "vtt" in sub.get("Format", "").lower() else "webvtt",
                        })
        cla_info = item.get("cla_info") or video_data.get("claInfo") or {}
        if isinstance(cla_info, dict):
            captions = cla_info.get("captions") or []
            if isinstance(captions, list):
                for c in captions:
                    if isinstance(c, dict):
                        lang = c.get("lang") or "en"
                        c_url = c.get("url") or ""
                        if c_url:
                            subtitles.setdefault(lang, []).append({
                                "url": c_url,
                                "lang": c.get("lang_name") or lang,
                                "ext": "vtt",
                            })

        if not play_addr and not hd_play_addr and not download_addr:
            return None

        download_link = {}
        play_identity = _media_identity(play_addr_data)
        if play_addr:
            key_sd = create_session({
                "url": canonical_url,
                "type": "video",
                "quality": "no_watermark",
                "direct_url": play_addr,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "duration": duration,
                "cookies": cookies,
            })
            download_link["no_watermark"] = f"/tiktok/download?key={key_sd}"

        if hd_play_addr and (not play_addr or hd_play_identity != play_identity):
            key_hd = create_session({
                "url": canonical_url,
                "type": "video",
                "quality": "no_watermark_hd",
                "direct_url": hd_play_addr,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "duration": duration,
                "cookies": cookies,
            })
            download_link["no_watermark_hd"] = f"/tiktok/download?key={key_hd}"
            if "no_watermark" not in download_link:
                download_link["no_watermark"] = download_link["no_watermark_hd"]

        has_watermark = bool(video_data.get("hasWatermark") or video_data.get("has_watermark"))
        download_identity = _media_identity(download_addr_data)
        known_identities = {identity for identity in (play_identity, hd_play_identity) if identity}
        if download_addr and has_watermark and download_identity not in known_identities:
            key_wm = create_session({
                "url": canonical_url,
                "type": "video",
                "quality": "watermark",
                "direct_url": download_addr,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "duration": duration,
                "cookies": cookies,
            })
            download_link["watermark"] = f"/tiktok/download?key={key_wm}"

        if audio_url:
            key_mp3 = create_session({
                "url": canonical_url,
                "type": "mp3",
                "direct_url": audio_url,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "duration": music_duration or duration,
                "cookies": cookies,
            })
            download_link["mp3"] = f"/tiktok/download?key={key_mp3}"

        res_dict = {
            "status": "tunnel",
            "extract_source": source,
            "title": title,
            "description": description,
            "statistics": statistics,
            "artist": nickname,
            "cover": cover,
            "dynamic_cover": dynamic_cover,
            "duration": duration,
            "audio": audio_url,
            "download_link": download_link,
            "music_duration": music_duration or duration,
            "author": {
                "nickname": nickname,
                "uniqueId": unique_id,
                "signature": author_data.get("signature") or "",
                "avatar": avatar,
                "avatarThumb": avatar,
                "avatarMedium": avatar,
                "avatarLarger": avatar,
            },
        }
        if subtitles:
            res_dict["subtitles"] = subtitles
        return res_dict

    def build_response_from_frontity(self, vdata: Dict[str, Any], canonical_url: str, source: str = "web_embed_ssr", cookies: str = "") -> Dict[str, Any]:
        item = vdata.get("itemInfos", {})
        author = vdata.get("authorInfos", {})
        music = vdata.get("musicInfos", {})
        stats = vdata.get("authorStats", {})
        image_post = vdata.get("imagePostInfo")

        title = item.get("text") or ""
        nickname = author.get("nickName") or author.get("uniqueId") or "unknown"
        unique_id = author.get("uniqueId") or nickname
        safe_author = sanitize_filename_part(nickname)

        avatar_list = author.get("coversLarger") or author.get("coversMedium") or author.get("covers") or []
        avatar = avatar_list[0] if avatar_list else ""

        music_urls = music.get("playUrl") or []
        music_url = music_urls[0] if music_urls else ""

        covers = item.get("covers") or []
        cover = covers[0] if covers else ""

        # Case 1: Photo Slideshow
        if image_post:
            images = image_post.get("displayImages") or image_post.get("images") or []
            image_urls = []
            for img in images:
                u = _first_url(img.get("displayImage") or img.get("urlList") or img.get("imageURL") or img)
                if u:
                    image_urls.append(u)

            if image_urls:
                photos = []
                for idx, img_url in enumerate(image_urls, start=1):
                    key_photo = create_session({
                        "url": canonical_url,
                        "type": "photo",
                        "photo_index": idx,
                        "direct_url": img_url,
                        "author": safe_author,
                        "proxy": self.proxy,
                        "impersonate": self.impersonate,
                        "cookies": cookies,
                    })
                    link = f"/tiktok/download?key={key_photo}"
                    photos.append({
                        "type": "photo",
                        "url": img_url,
                        "download_link": link,
                    })

                download_link = {
                    "no_watermark": [p["download_link"] for p in photos],
                }
                if music_url:
                    key_mp3 = create_session({
                        "url": canonical_url,
                        "type": "mp3",
                        "direct_url": music_url,
                        "author": safe_author,
                        "proxy": self.proxy,
                        "impersonate": self.impersonate,
                        "cookies": cookies,
                    })
                    download_link["mp3"] = f"/tiktok/download?key={key_mp3}"

                slideshow_key = create_session({
                    "url": canonical_url,
                    "type": "slideshow",
                    "photo_urls": image_urls,
                    "audio_url": music_url,
                    "author": safe_author,
                    "proxy": self.proxy,
                    "impersonate": self.impersonate,
                    "cookies": cookies,
                })

                return {
                    "status": "picker",
                    "extract_source": source,
                    "title": title,
                    "description": title,
                    "statistics": {
                        "play_count": item.get("playCount", 0),
                        "digg_count": item.get("diggCount", 0),
                        "comment_count": item.get("commentCount", 0),
                        "share_count": item.get("shareCount", 0),
                    },
                    "artist": nickname,
                    "cover": image_urls[0] if image_urls else cover,
                    "duration": len(image_urls) * 3,
                    "audio": music_url,
                    "download_link": download_link,
                    "photos": photos,
                    "download_slideshow": f"/tiktok/download?key={slideshow_key}",
                    "download_slideshow_link": f"/tiktok/download?key={slideshow_key}",
                    "author": {
                        "nickname": nickname,
                        "uniqueId": unique_id,
                        "signature": author.get("signature", ""),
                        "avatar": avatar,
                        "avatarThumb": avatar,
                        "avatarMedium": avatar,
                        "avatarLarger": avatar,
                    },
                }

        # Case 2: Video
        video_obj = item.get("video") or {}
        video_urls = video_obj.get("urls") or []
        video_url = video_urls[0] if video_urls else ""

        if not video_url:
            return None

        key_sd = create_session({
            "url": canonical_url,
            "type": "video",
            "quality": "no_watermark",
            "direct_url": video_url,
            "author": safe_author,
            "proxy": self.proxy,
            "impersonate": self.impersonate,
            "cookies": cookies,
        })

        download_link = {
            "no_watermark": f"/tiktok/download?key={key_sd}",
        }

        if music_url:
            key_mp3 = create_session({
                "url": canonical_url,
                "type": "mp3",
                "direct_url": music_url,
                "author": safe_author,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "cookies": cookies,
            })
            download_link["mp3"] = f"/tiktok/download?key={key_mp3}"

        return {
            "status": "tunnel",
            "extract_source": source,
            "title": title,
            "description": title,
            "statistics": {
                "play_count": item.get("playCount", 0),
                "digg_count": item.get("diggCount", 0),
                "comment_count": item.get("commentCount", 0),
                "share_count": item.get("shareCount", 0),
            },
            "artist": nickname,
            "cover": cover,
            "dynamic_cover": cover,
            "duration": item.get("video", {}).get("duration", 0),
            "audio": music_url,
            "download_link": download_link,
            "music_duration": music.get("duration", 0),
            "author": {
                "nickname": nickname,
                "uniqueId": unique_id,
                "signature": author.get("signature", ""),
                "avatar": avatar,
                "avatarThumb": avatar,
                "avatarMedium": avatar,
                "avatarLarger": avatar,
            },
        }
