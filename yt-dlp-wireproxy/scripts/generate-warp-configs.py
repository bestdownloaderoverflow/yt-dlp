#!/usr/bin/env python3
"""
generate-warp-configs.py - Otomatis generate N akun Cloudflare WARP IPv6 mandiri untuk Wireproxy

Fitur:
1. Mendukung rotasi proxy (via curl_cffi / urllib) agar bebas rate-limit (429).
2. Auto-retry dengan exponential backoff.
3. Otomatis menghasilkan format Wireproxy siap pakai.
"""

import argparse
import base64
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests
    HAS_CURL_CFFI = True
except ImportError:
    import urllib.request
    HAS_CURL_CFFI = False


def generate_single_warp_config(proxy: str = None) -> dict:
    """Generate X25519 keypair and register a new WARP client device with Cloudflare."""
    # Generate X25519 private key using openssl
    priv_raw = subprocess.check_output(['openssl', 'genpkey', '-algorithm', 'X25519', '-outform', 'DER'])
    priv_bytes = priv_raw[-32:]
    priv_b64 = base64.b64encode(priv_bytes).decode('utf-8')

    # Derive public key
    p = subprocess.Popen(
        ['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    pub_raw, _ = p.communicate(input=priv_raw)
    pub_bytes = pub_raw[-32:]
    pub_b64 = base64.b64encode(pub_bytes).decode('utf-8')

    # Register with Cloudflare API
    url = 'https://api.cloudflareclient.com/v0a2158/reg'
    payload = {
        'key': pub_b64,
        'install_id': '',
        'fcm_token': '',
        'tos': datetime.now(timezone.utc).isoformat()[:-6] + 'Z',
        'model': 'PC',
        'serial_number': '',
        'locale': 'en_US'
    }
    headers = {
        'Content-Type': 'application/json; charset=UTF-8',
        'User-Agent': 'okhttp/3.12.1'
    }

    if HAS_CURL_CFFI:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=12, impersonate="chrome120")
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:100]}")
        data = resp.json()
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8'))

    v6 = data.get('config', {}).get('interface', {}).get('addresses', {}).get('v6')
    v4 = data.get('config', {}).get('interface', {}).get('addresses', {}).get('v4')
    account_id = data.get('id')
    peer = data.get('config', {}).get('peers', [{}])[0]
    peer_pub = peer.get('public_key', 'bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=')

    return {
        'account_id': account_id,
        'private_key': priv_b64,
        'public_key': pub_b64,
        'ipv4': v4,
        'ipv6': v6,
        'peer_pub': peer_pub,
        'endpoint': 'engage.cloudflareclient.com:2408'
    }


def format_wireproxy_conf(warp: dict, socks5_port: int = 1080) -> str:
    """Format into wireproxy config file."""
    v4 = warp['ipv4'] if warp['ipv4'].endswith('/32') else f"{warp['ipv4']}/32"
    v6 = warp['ipv6'] if warp['ipv6'].endswith('/128') else f"{warp['ipv6']}/128"
    return f"""[Interface]
# Cloudflare WARP Account ID: {warp['account_id']}
Address = {v4}, {v6}
PrivateKey = {warp['private_key']}
DNS = 1.1.1.1, 2606:4700:4700::1111

[Peer]
PublicKey = {warp['peer_pub']}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
Endpoint = {warp['endpoint']}

[Socks5]
BindAddress = 0.0.0.0:{socks5_port}
"""


def main():
    parser = argparse.ArgumentParser(description="Generate N unique Cloudflare WARP Wireproxy configs")
    parser.add_argument("--count", type=int, default=5, help="Jumlah konfigurasi yang ingin digenerate (default: 5)")
    parser.add_argument("--output-dir", type=str, default="runtime-configs", help="Direktori output")
    parser.add_argument("--start-index", type=int, default=12, help="Index awal penomoran file (default: 12)")
    parser.add_argument("--use-proxies", action="store_true", help="Gunakan proxy pool yang ada untuk bypass rate-limit")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("════════════════════════════════════════════════════════════════════════════════")
    print(f"  ⚡ GENERATING {args.count} INDEPENDENT CLOUDFLARE WARP IPV6 CONFIGS")
    print("════════════════════════════════════════════════════════════════════════════════")

    # Proxy list for registration rotation if needed
    avail_proxies = []
    if args.use_proxies:
        for p in range(1, 19):
            avail_proxies.append(f"socks5h://127.0.0.1:{10800 + p}")

    generated = []
    for i in range(args.start_index, args.start_index + args.count):
        print(f"⏳ Generating WARP Node #{i:02d}...", end=" ", flush=True)
        max_retries = 5
        success = False
        
        for attempt in range(1, max_retries + 1):
            proxy = random.choice(avail_proxies) if avail_proxies else None
            try:
                w = generate_single_warp_config(proxy=proxy)
                conf_str = format_wireproxy_conf(w, socks5_port=1080)
                file_name = f"wireproxy-{i:02d}.conf"
                file_path = out_dir / file_name
                with open(file_path, "w") as f:
                    f.write(conf_str)
                print(f"✅ OK! IPv6: {w['ipv6']}")
                generated.append((file_name, w['ipv6'], w['account_id']))
                success = True
                break
            except Exception as e:
                if "429" in str(e):
                    wait_sec = attempt * 8
                    print(f"\n   ⚠️ Rate limited (429). Menunggu {wait_sec}s lalu retry (attempt {attempt}/{max_retries})...", end=" ", flush=True)
                    time.sleep(wait_sec)
                else:
                    print(f"❌ Error: {e}")
                    time.sleep(2)
                    break
        
        if not success:
            print(f"❌ Gagal setelah {max_retries} percobaan.")
        else:
            time.sleep(1)

    print("\n" + "─" * 80)
    print(f"🎉 Sukses meng-generate {len(generated)} file konfigurasi WARP unik di folder: {out_dir}")
    print("─" * 80)
    for fname, ip6, aid in generated:
        print(f"  📁 {fname:<25} ➔ IPv6: {ip6}")
    print("─" * 80)


if __name__ == "__main__":
    main()
