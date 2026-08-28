#!/usr/bin/env python3
"""
generate-wireproxy-configs.py

Builds runtime-configs/ by pairing each VPN *account key* with a randomly chosen
*server*, gluetun-style: you say which countries are allowed, the generator picks
the actual servers from each provider's live server list.

This matters because a WireGuard account key is not bound to the server it was
issued for. Pinning every Surfshark key to id-jak.prod.surfshark.com made all
those nodes leave from one shared exit IP; spreading the same keys over
different servers gives one distinct exit per server.

Index layout is a contract shared with tiktok-ssr-engine/config.py and
docker-compose.wireproxy.yml:

    wireproxy-01 .. wireproxy-12  Surfshark  (keys from ../surfshark-pay)
    wireproxy-13 .. wireproxy-17  Mullvad    (keys from ../mullvad-new)
    wireproxy-18 .. wireproxy-21  Cloudflare (profiles from ../cloudflare-warp)

Indonesia-pinned nodes are placed first within each provider block, so their
indexes stay predictable and can be fed to INDONESIA_PROXIES.

Re-run to reroll the server assignment, then restart the containers:

    ./scripts/generate-wireproxy-configs.py
    docker compose -f docker-compose.wireproxy.yml restart
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = PROJECT_DIR.parent

SURFSHARK_DIR = ROOT_DIR / "surfshark-pay"
MULLVAD_DIR = ROOT_DIR / "mullvad-new"
WARP_DIR = ROOT_DIR / "cloudflare-warp"
DEST_DIR = PROJECT_DIR / "runtime-configs"
POOL_MANIFEST = DEST_DIR / "pool.json"
CACHE_DIR = PROJECT_DIR / "scripts" / ".server-cache"

MULLVAD_API = "https://api.mullvad.net/www/relays/wireguard/"
SURFSHARK_API = "https://api.surfshark.com/v4/server/clusters/all"

# Index ranges per provider (1-based, inclusive) -- mirrored in config.py.
SURFSHARK_SLOTS = 12
MULLVAD_SLOTS = 5
WARP_SLOTS = 4

# HK is deliberately absent: TikTok 302s Hong Kong exits to /hk/about instead of
# serving video pages, so those nodes cannot scrape.
DEFAULT_COUNTRIES = ["ID", "SG", "VN", "MY", "TH", "PH"]
DEFAULT_INDONESIA_NODES = 3

WIREGUARD_PORT = 51820
SOCKS_SECTION = "\n[Socks5]\nBindAddress = 0.0.0.0:1080\n"


class Server:
    """One VPN server a key can be pointed at."""

    def __init__(self, provider: str, name: str, endpoint: str, pubkey: str,
                 country: str, city: str):
        self.provider = provider
        self.name = name
        self.endpoint = endpoint
        self.pubkey = pubkey
        self.country = country
        self.city = city

    def __repr__(self) -> str:
        return f"{self.name} ({self.country}/{self.city})"


def fetch_json(url: str, cache: Path, offline: bool):
    """Fetch a server list, falling back to the on-disk cache."""
    if offline and cache.exists():
        return json.loads(cache.read_text())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wireproxy-config-generator"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
        return data
    except Exception as exc:  # noqa: BLE001 - any network failure falls back to cache
        if cache.exists():
            print(f"WARNING: {url} failed ({exc}); using cached list", file=sys.stderr)
            return json.loads(cache.read_text())
        raise SystemExit(f"ERROR: cannot fetch {url} and no cache at {cache}: {exc}")


def load_mullvad_servers(countries: list[str], offline: bool) -> list[Server]:
    data = fetch_json(MULLVAD_API, CACHE_DIR / "mullvad.json", offline)
    servers = []
    for r in data:
        if not r.get("active"):
            continue
        cc = (r.get("country_code") or "").upper()
        if cc not in countries:
            continue
        servers.append(Server(
            provider="mullvad",
            name=r["hostname"],
            # Mullvad publishes the relay IP directly, so no DNS lookup is needed.
            endpoint=f"{r['ipv4_addr_in']}:{WIREGUARD_PORT}",
            pubkey=r["pubkey"],
            country=cc,
            city=r.get("city_name", "?"),
        ))
    return servers


def load_surfshark_servers(countries: list[str], offline: bool) -> list[Server]:
    data = fetch_json(SURFSHARK_API, CACHE_DIR / "surfshark.json", offline)
    servers = []
    for x in data:
        cc = (x.get("countryCode") or "").upper()
        # Obfuscated clusters ship no public key and cannot be used here.
        if cc not in countries or not x.get("pubKey"):
            continue
        servers.append(Server(
            provider="surfshark",
            name=x["connectionName"],
            endpoint=f"{x['connectionName']}:{WIREGUARD_PORT}",
            pubkey=x["pubKey"],
            country=cc,
            city=x.get("location", "?"),
        ))
    return servers


def parse_account(path: Path) -> dict:
    """Pull the account-side fields (key, address, DNS) out of a WireGuard config."""
    text = path.read_text()

    def field(name: str) -> str | None:
        m = re.search(rf"^{name}\s*=\s*(.+)$", text, re.MULTILINE)
        return m.group(1).strip() if m else None

    private_key = field("PrivateKey")
    address = field("Address")
    if not private_key or not address:
        raise SystemExit(f"ERROR: {path} has no PrivateKey/Address")
    return {
        "source": path,
        "private_key": private_key,
        "address": address,
        "dns": field("DNS"),
    }


def assign_servers(accounts: list[dict], servers: list[Server], indonesia_needed: int,
                   rng: random.Random, label: str) -> list[tuple[dict, Server]]:
    """
    Give every account a server: Indonesia slots first, then the rest spread over
    the remaining countries. Servers are used at most once while any are unused.
    """
    if not servers:
        raise SystemExit(f"ERROR: no {label} servers matched the country filter")

    indo = [s for s in servers if s.country == "ID"]
    rest = [s for s in servers if s.country != "ID"]
    rng.shuffle(indo)
    rng.shuffle(rest)

    if indonesia_needed > len(indo):
        print(f"WARNING: {label}: asked for {indonesia_needed} Indonesia nodes but only "
              f"{len(indo)} ID servers exist; the rest will reuse them", file=sys.stderr)

    chosen: list[Server] = []
    for i in range(indonesia_needed):
        chosen.append(indo[i % len(indo)] if indo else rest[i % len(rest)])

    # Spread the non-Indonesia picks round-robin across countries so neighbouring
    # indexes land in different regions.
    by_country: dict[str, list[Server]] = defaultdict(list)
    for s in rest:
        by_country[s.country].append(s)
    order = sorted(by_country, key=lambda c: (-len(by_country[c]), c))

    spread: list[Server] = []
    while any(by_country[c] for c in order):
        for c in order:
            if by_country[c]:
                spread.append(by_country[c].pop(0))

    remaining = len(accounts) - indonesia_needed
    if remaining > len(spread):
        print(f"WARNING: {label}: {remaining} nodes but only {len(spread)} non-ID servers; "
              f"some servers will be shared", file=sys.stderr)
    for i in range(remaining):
        chosen.append(spread[i % len(spread)] if spread else indo[i % len(indo)])

    return list(zip(accounts, chosen))


def write_pool_manifest(entries: list[dict]) -> None:
    """Publish the pool layout for tiktok-ssr-engine to read.

    The .conf files carry the country only in a comment, so the engine had no
    way to know where a node exits. That left it inferring geo-restriction from
    "two distinct exit IPv4s failed" -- which proves nothing when most of the
    pool sits in one country. This manifest is the single source of truth for
    that, and it also lets INDONESIA_PROXIES be derived instead of hand-copied.
    """
    POOL_MANIFEST.write_text(json.dumps({
        "version": 1,
        "proxies": entries,
    }, indent=2) + "\n")
    # World-readable on purpose: unlike the .conf files this holds no key
    # material, and the engine container mounts it as an unprivileged reader.
    POOL_MANIFEST.chmod(0o644)


def render(account: dict, server: Server) -> str:
    lines = ["[Interface]", f"PrivateKey = {account['private_key']}", f"Address = {account['address']}"]
    if account["dns"]:
        lines.append(f"DNS = {account['dns']}")
    lines += [
        "",
        f"# {server.provider}: {server.name} [{server.country}/{server.city}]",
        "[Peer]",
        f"PublicKey = {server.pubkey}",
        "AllowedIPs = 0.0.0.0/0",
        # Without this the tunnel goes idle and the next packet pays a ~15s
        # re-handshake, which times out the health prober and marks healthy
        # exits dead. Keep the tunnel warm instead.
        "PersistentKeepalive = 25",
        f"Endpoint = {server.endpoint}",
    ]
    return "\n".join(lines) + "\n" + SOCKS_SECTION


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES),
                   help=f"comma-separated country codes to draw servers from (default: {','.join(DEFAULT_COUNTRIES)})")
    p.add_argument("--indonesia", type=int, default=DEFAULT_INDONESIA_NODES,
                   help=f"how many nodes to pin to Indonesia (default: {DEFAULT_INDONESIA_NODES})")
    p.add_argument("--seed", type=int, default=None, help="seed the picker for a reproducible layout")
    p.add_argument("--offline", action="store_true", help="use the cached server lists instead of fetching")
    args = p.parse_args()

    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    rng = random.Random(args.seed)

    for d in (SURFSHARK_DIR, MULLVAD_DIR, WARP_DIR):
        if not d.is_dir():
            raise SystemExit(f"ERROR: source directory not found: {d}")

    surfshark_accounts = [parse_account(f) for f in sorted(SURFSHARK_DIR.rglob("*.conf"))]
    mullvad_accounts = [parse_account(f) for f in sorted(MULLVAD_DIR.glob("*.conf"))]
    warp_profiles = sorted(WARP_DIR.glob("*.conf"))

    for label, got, want in (("Surfshark", len(surfshark_accounts), SURFSHARK_SLOTS),
                             ("Mullvad", len(mullvad_accounts), MULLVAD_SLOTS),
                             ("Cloudflare", len(warp_profiles), WARP_SLOTS)):
        if got != want:
            print(f"WARNING: expected {want} {label} configs, found {got}", file=sys.stderr)

    surfshark_servers = load_surfshark_servers(countries, args.offline)
    mullvad_servers = load_mullvad_servers(countries, args.offline)
    print(f"Server pool: {len(surfshark_servers)} Surfshark, {len(mullvad_servers)} Mullvad "
          f"across {','.join(countries)}")

    # Indonesia quota is split across the two geo providers, favouring whichever
    # has more ID servers so each Indonesia node gets a distinct one.
    ss_id = len([s for s in surfshark_servers if s.country == "ID"])
    mv_id = len([s for s in mullvad_servers if s.country == "ID"])
    mv_indo = min(mv_id, args.indonesia)
    ss_indo = min(ss_id, args.indonesia - mv_indo)
    if mv_indo + ss_indo < args.indonesia:
        print(f"WARNING: only {mv_indo + ss_indo} distinct Indonesia servers available, "
              f"{args.indonesia} requested", file=sys.stderr)

    pairs = (assign_servers(surfshark_accounts, surfshark_servers, ss_indo, rng, "Surfshark")
             + assign_servers(mullvad_accounts, mullvad_servers, mv_indo, rng, "Mullvad"))

    if DEST_DIR.exists():
        for old in DEST_DIR.glob("*.conf"):
            old.unlink()
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR.chmod(0o700)

    print("\n=== Generated configs ===")
    indonesia_indexes = []
    pool_entries = []
    idx = 0

    for account, server in pairs:
        idx += 1
        dest = DEST_DIR / f"wireproxy-{idx:02d}.conf"
        dest.write_text(render(account, server))
        dest.chmod(0o600)
        if server.country == "ID":
            indonesia_indexes.append(idx)
        pool_entries.append({
            "index": idx,
            "provider": server.provider,
            "country": server.country,
            "city": server.city,
            "server": server.name,
            "endpoint": server.endpoint,
        })
        print(f"  wireproxy-{idx:02d}  {server.provider:<9} {server.name:<38} "
              f"[{server.country}/{server.city}]  key={account['source'].name}")

    for profile in warp_profiles:
        idx += 1
        dest = DEST_DIR / f"wireproxy-{idx:02d}.conf"
        text = profile.read_text()
        if "[Socks5]" not in text:
            text = text.rstrip("\n") + "\n" + SOCKS_SECTION
        dest.write_text(text)
        dest.chmod(0o600)
        pool_entries.append({
            "index": idx,
            "provider": "cloudflare",
            # WARP is anycast: the egress country depends on which colo answers
            # and can change between handshakes, so there is no country to pin.
            # null means unknown, which the engine treats as "cannot be used as
            # country evidence" rather than as a country of its own.
            "country": None,
            "city": None,
            "server": "warp anycast",
            "endpoint": "engage.cloudflareclient.com:2408",
        })
        print(f"  wireproxy-{idx:02d}  cloudflare {'warp anycast':<38} [--/--]  key={profile.name}")

    write_pool_manifest(pool_entries)

    unique = len({(s.provider, s.name) for _, s in pairs})
    print(f"\nDone. {idx} configs written to {DEST_DIR}")
    print(f"Distinct geo servers: {unique} across {len(pairs)} geo nodes")
    print(f"Wrote {POOL_MANIFEST.name}: the engine reads country per node from it, "
          "so INDONESIA_PROXIES no longer has to be copied by hand.")
    print(f"Indonesia nodes: {','.join(str(i) for i in indonesia_indexes)}  "
          "(derived automatically; set INDONESIA_PROXIES only to override)")
    by_country = defaultdict(int)
    for entry in pool_entries:
        by_country[entry["country"] or "??"] += 1
    print("Country spread: " + ", ".join(
        f"{cc}={n}" for cc, n in sorted(by_country.items(), key=lambda kv: (-kv[1], kv[0]))))
    print("Run: docker compose -f docker-compose.wireproxy.yml restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
