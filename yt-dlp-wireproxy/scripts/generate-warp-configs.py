#!/usr/bin/env python3
"""
generate-warp-configs.py - Otomatis generate N akun Cloudflare WARP IPv6 mandiri untuk Wireproxy

Usage:
  python3 scripts/generate-warp-configs.py --count 5 --output-dir runtime-configs/
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def generate_single_warp_config() -> dict:
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

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json; charset=UTF-8',
            'User-Agent': 'okhttp/3.12.1'
        },
        method='POST'
    )

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
    return f"""[Interface]
# Cloudflare WARP Account ID: {warp['account_id']}
Address = {warp['ipv4']}, {warp['ipv6']}
PrivateKey = {warp['private_key']}
DNS = 1.1.1.1, 2606:4700:4700::1111

[Peer]
PublicKey = {warp['peer_pub']}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {warp['endpoint']}

[Socks5]
BindAddress = 0.0.0.0:{socks5_port}
"""


def main():
    parser = argparse.ArgumentParser(description="Generate N unique Cloudflare WARP Wireproxy configs")
    parser.add_argument("--count", type=int, default=5, help="Jumlah konfigurasi yang ingin digenerate (default: 5)")
    parser.add_argument("--output-dir", type=str, default="runtime-configs/warp", help="Direktori output")
    parser.add_argument("--start-index", type=int, default=1, help="Index awal penomoran file (default: 1)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("════════════════════════════════════════════════════════════════════════════════")
    print(f"  ⚡ GENERATING {args.count} INDEPENDENT CLOUDFLARE WARP IPV6 CONFIGS")
    print("════════════════════════════════════════════════════════════════════════════════")

    generated = []
    for i in range(args.start_index, args.start_index + args.count):
        print(f"⏳ Generating WARP Node #{i:02d}...", end=" ", flush=True)
        try:
            w = generate_single_warp_config()
            conf_str = format_wireproxy_conf(w, socks5_port=1080)
            file_name = f"wireproxy-warp-{i:02d}.conf"
            file_path = out_dir / file_name
            with open(file_path, "w") as f:
                f.write(conf_str)
            print(f"✅ OK! IPv6: {w['ipv6']}")
            generated.append((file_name, w['ipv6'], w['account_id']))
        except Exception as e:
            print(f"❌ Error: {e}")

    print("\n" + "─" * 80)
    print(f"🎉 Sukses meng-generate {len(generated)} file konfigurasi WARP unik di folder: {out_dir}")
    print("─" * 80)
    for fname, ip6, aid in generated:
        print(f"  📁 {fname:<25} ➔ IPv6: {ip6}")
    print("─" * 80)


if __name__ == "__main__":
    main()
