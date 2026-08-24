#!/usr/bin/env python3
"""
test_ipv6_tiktok.py - Mini program untuk menguji ketahanan scraping SSR TikTok via IPv6 vs IPv4

Fitur Pengujian:
1. Mendeteksi IP Outbound (apakah request benar-benar keluar via IPv6 atau IPv4).
2. Menguji scraping Web SSR (Universal Data) & Embed SSR (Frontity).
3. Mengukur latency (ms), ukuran respon (bytes), dan status WAF Challenge.
4. Menjalankan benchmark berulang (stress-test) untuk melihat ban-rate.

Usage:
  python3 test_ipv6_tiktok.py                           # Test default via IPv6 langsung
  python3 test_ipv6_tiktok.py --proxy socks5h://...    # Test via proxy tertentu (misal WARP IPv6)
  python3 test_ipv6_tiktok.py --count 10               # Jalankan 10 request berturut-turut
"""

import argparse
import asyncio
import json
import re
import sys
import time
from typing import Dict, Any, Optional

try:
    from curl_cffi.requests import AsyncSession
except ImportError:
    print("❌ Error: modul 'curl_cffi' belum terinstall. Jalankan: pip install curl_cffi")
    sys.exit(1)

TEST_VIDEOS = [
    ("Global Video", "https://www.tiktok.com/@jjtrailwalker/video/7660242147043544334", "7660242147043544334"),
    ("Photo Slideshow", "https://www.tiktok.com/@yusuf_sufiandi24/photo/7626300866928135444", "7626300866928135444"),
    ("Regional Video", "https://www.tiktok.com/@vidiosports/video/7324033260227480838", "7324033260227480838"),
]

DESKTOP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


async def detect_outbound_ip(session: AsyncSession) -> Dict[str, str]:
    """Cek IP outbound dan jenis protokol (IPv4 atau IPv6)."""
    try:
        r = await session.get("https://api64.ipify.org?format=json", timeout=6)
        ip = r.json().get("ip", "unknown")
        proto = "IPv6" if ":" in ip else "IPv4"
        return {"ip": ip, "proto": proto}
    except Exception as e:
        return {"ip": f"error ({e})", "proto": "unknown"}


async def scrape_tiktok_test(session: AsyncSession, label: str, url: str, item_id: str) -> Dict[str, Any]:
    """Uji coba scraping SSR ke TikTok."""
    t0 = time.time()
    result = {
        "label": label,
        "url": url,
        "success": False,
        "method": "-",
        "status_code": 0,
        "latency_ms": 0,
        "waf_challenge": False,
        "has_video_url": False,
        "title": "",
        "error": "",
    }

    headers = dict(DESKTOP_HEADERS)
    headers["Referer"] = "https://www.tiktok.com/"

    # --- Strategy 1: Desktop Web SSR ---
    try:
        resp = await session.get(url, headers=headers, timeout=10)
        result["status_code"] = resp.status_code
        html = resp.text

        if "SlardarWAF" in html or "_wafchallengeid" in html:
            result["waf_challenge"] = True

        match = re.search(r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"\s+type="application/json">([^<]+)</script>', html)
        if match:
            data = json.loads(match.group(1))
            detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
            item = detail.get("itemInfo", {}).get("itemStruct")
            if item:
                result["success"] = True
                result["method"] = "Web SSR (__UNIVERSAL_DATA__)"
                result["title"] = (item.get("desc") or "")[:40]
                result["has_video_url"] = bool(item.get("video", {}).get("playAddr") or item.get("imagePost"))
                result["latency_ms"] = round((time.time() - t0) * 1000, 1)
                return result
    except Exception as e:
        result["error"] = str(e)

    # --- Strategy 2: Official Embed SSR ---
    try:
        embed_url = f"https://www.tiktok.com/embed/v2/{item_id}"
        resp_embed = await session.get(embed_url, headers=headers, timeout=10)
        match_embed = re.search(r'<script\s+id="__FRONTITY_CONNECT_STATE__"\s+type="application/json">([^<]+)</script>', resp_embed.text)
        if match_embed:
            d = json.loads(match_embed.group(1))
            source = d.get("source", {})
            data = source.get("data", {})
            for k, v in data.items():
                if isinstance(v, dict) and "videoData" in v:
                    vdata = v["videoData"]
                    item = vdata.get("itemInfos", {})
                    if item and item.get("text"):
                        result["success"] = True
                        result["method"] = "Embed SSR (__FRONTITY__)"
                        result["title"] = (item.get("text") or "")[:40]
                        result["has_video_url"] = bool(item.get("video", {}).get("urls") or vdata.get("imagePostInfo"))
                        result["latency_ms"] = round((time.time() - t0) * 1000, 1)
                        return result
    except Exception as e:
        if not result["error"]:
            result["error"] = str(e)

    result["latency_ms"] = round((time.time() - t0) * 1000, 1)
    return result


async def run_single_cycle(proxy: Optional[str] = None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
        ip_info = await detect_outbound_ip(session)
        print(f"\n🌐 Outbound IP: \033[1;36m{ip_info['ip']}\033[0m (Protokol: \033[1;33m{ip_info['proto']}\033[0m | Proxy: {proxy or 'Direct VPS'})")
        print("─" * 80)
        print(f"{'Kategori':<18} | {'Status':<10} | {'Latency':<8} | {'Metode':<22} | {'Judul Konten'}")
        print("─" * 80)

        for label, url, item_id in TEST_VIDEOS:
            res = await scrape_tiktok_test(session, label, url, item_id)
            if res["success"]:
                status_str = "\033[1;32m✓ SUKSES\033[0m"
                title_str = f"{res['title']}..."
            else:
                status_str = "\033[1;31m✗ GAGAL\033[0m"
                title_str = f"Error: {res['error'] or 'WAF/Geo-block'}"

            waf_flag = " [WAF Challenge]" if res["waf_challenge"] else ""
            print(f"{label:<18} | {status_str:<19} | {res['latency_ms']:>5} ms  | {res['method']:<22} | {title_str}{waf_flag}")
        print("─" * 80)


async def main():
    parser = argparse.ArgumentParser(description="Test Scraping SSR TikTok via IPv6 vs IPv4")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL (misal: socks5h://wireproxy-13:1080 atau http://127.0.0.1:8888)")
    parser.add_argument("--count", type=int, default=1, help="Jumlah iterasi test")
    args = parser.parse_args()

    print("════════════════════════════════════════════════════════════════════════════════")
    print("  🚀 TIKTOK SSR IPV6 VS IPV4 SCRAPING TESTER")
    print("════════════════════════════════════════════════════════════════════════════════")

    for i in range(1, args.count + 1):
        if args.count > 1:
            print(f"\n[Siklus {i}/{args.count}]")
        await run_single_cycle(args.proxy)
        if i < args.count:
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
