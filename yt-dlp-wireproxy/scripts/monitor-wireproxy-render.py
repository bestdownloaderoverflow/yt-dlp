#!/usr/bin/env python3
"""Render tunnel health plus TikTok-specific proxy rotation state.

Reads from a per-cycle temp directory:
  health.json   - output of GET /health
  proxies.json  - authenticated output of GET /proxies
  m-XX          - text/wg-show output from wireproxy-XX /metrics
  ip-XX         - IPv4 exit captured from SOCKS5 probe
  ipv6-XX       - optional IPv6 exit for WARP nodes
  lat-XX        - "<http_code> <time_total>" captured from SOCKS5 probe
  restart-XX    - latest restart_log:pN Redis entry

Renders a colored ASCII table grouped by country, sorted by index, to stdout.
Auto-strips ANSI when stdout is not a TTY (so piping to a file/log still works).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

# --- ANSI helpers ------------------------------------------------------------

_RST = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GRN = "\033[32m"
_YEL = "\033[33m"
_BLU = "\033[34m"
_MAG = "\033[35m"
_CYN = "\033[36m"
_GRY = "\033[90m"

USE_COLOR = sys.stdout.isatty() and not os.environ.get("MON_NO_COLOR")


def c(code: str, text: str) -> str:
    return f"{code}{text}{_RST}" if USE_COLOR else text


# --- Regexes -----------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")

RE_ENDPOINT = re.compile(r"endpoint:\s+(\S+)")
RE_HANDSHAKE = re.compile(r"latest handshake:\s+(.+)")
RE_TRANSFER = re.compile(
    r"transfer:\s+([\d.]+)\s*(\w*?)\s*received,\s+([\d.]+)\s*(\w*?)\s*sent"
)

UNIT_FACTOR = {
    "b": 1, "byte": 1, "bytes": 1,
    "kib": 1024, "kb": 1000, "k": 1000, "ki": 1024,
    "mib": 1024 ** 2, "mb": 1000 ** 2, "m": 1000 ** 2, "mi": 1024 ** 2,
    "gib": 1024 ** 3, "gb": 1000 ** 3, "g": 1000 ** 3, "gi": 1024 ** 3,
    "tib": 1024 ** 4, "tb": 1000 ** 4,
}

COUNTRY_ORDER = {
    "Cloudflare WARP": 0,
    "Mullvad VPN": 1,
    "Surfshark VPN": 2,
    "Indonesia": 3,
    "Singapore": 4,
}


# --- Parsers -----------------------------------------------------------------

def parse_metrics(text: str) -> dict:
    out = {"endpoint": "", "handshake": "", "rx_b": 0, "tx_b": 0}
    if not text:
        return out
    text = _TAG_RE.sub("", text)  # strip HTML tags from wireproxy info page
    em = RE_ENDPOINT.search(text)
    if em:
        out["endpoint"] = em.group(1)
    hm = RE_HANDSHAKE.search(text)
    if hm:
        out["handshake"] = hm.group(1).strip()
    tm = RE_TRANSFER.search(text)
    if tm:
        try:
            rx = float(tm.group(1)) * UNIT_FACTOR.get(tm.group(2).lower(), 1)
            tx = float(tm.group(3)) * UNIT_FACTOR.get(tm.group(4).lower(), 1)
            out["rx_b"] = int(rx)
            out["tx_b"] = int(tx)
        except (ValueError, IndexError):
            pass
    return out


def parse_latency_file(text: str) -> tuple[int, float]:
    if not text:
        return 0, 0.0
    try:
        parts = text.strip().split()
        if len(parts) < 2:
            return 0, 0.0
        return int(parts[0]), float(parts[1])
    except (ValueError, IndexError):
        return 0, 0.0


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    for unit in ("K", "M", "G", "T"):
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}P"


def safe_read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return ""


def _format_duration_ms(duration_ms: int) -> str:
    if duration_ms < 0:
        return "-"
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"


def parse_restart_message(text: str) -> dict:
    out = {"message": "-", "success": None}
    if not text.strip():
        return out
    try:
        entry = json.loads(text)
    except json.JSONDecodeError:
        return out

    success = bool(entry.get("success"))
    result = "ok" if success else "fail"
    reason = str(entry.get("reason_code") or "unknown")
    duration = _format_duration_ms(int(entry.get("duration_ms", -1) or -1))
    restart_failures = int(entry.get("restart_failures", 0) or 0)
    ts = int(entry.get("timestamp", 0) or 0)
    when = ""
    if ts > 0:
        try:
            when = datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            when = ""

    bits = []
    if when:
        bits.append(when)
    bits.append(result)
    bits.append(reason)
    bits.append(duration)
    if restart_failures > 0:
        bits.append(f"rf={restart_failures}")
    if entry.get("quarantine"):
        bits.append("quarantine")

    out["message"] = " ".join(bits)
    out["success"] = success
    return out


# --- Handshake color ---------------------------------------------------------

def _handshake_age_seconds(handshake: str) -> int | None:
    """Parse 'X seconds/minutes/hours ago' or 'never' into seconds.

    Returns None if unparseable.
    """
    if not handshake or handshake.lower() == "never":
        return None
    m = re.match(r"(\d+)\s+(second|minute|hour|day)s?\s+ago", handshake)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "second":
        return n
    if unit == "minute":
        return n * 60
    if unit == "hour":
        return n * 3600
    if unit == "day":
        return n * 86400
    return None


def handshake_color(handshake: str) -> str:
    if not handshake or handshake == "-":
        return _DIM
    if handshake.lower() == "never":
        return _RED
    age = _handshake_age_seconds(handshake)
    if age is None:
        return _DIM
    if age <= 180:  # <= 3 minutes
        return _GRN
    if age <= 600:  # <= 10 minutes
        return _YEL
    return _RED


# --- Row-level status color --------------------------------------------------

def status_palette(row: dict) -> str:
    """Color the row's overall indicator (used in STATUS cell)."""
    if row["dead"]:
        return _RED
    if not row["state_known"]:
        return _RED
    if row["quarantined"]:
        return _RED
    if row["stabilizing"]:
        return _CYN
    if row["reconnecting"]:
        return _CYN
    if row["draining"]:
        return _YEL
    if row["restart_backoff"] > 0:
        return _YEL
    if row["restart_scheduled"]:
        return _YEL
    if row["blocked"]:
        return _RED
    if row["cooldown"]:
        return _YEL
    if row["active_requests"] > 0:
        return _YEL
    if not row["probe_ok"]:
        return _YEL
    if row["latency_ms"] > 3000:
        return _YEL
    if row["latency_ms"] > 1500:
        return _YEL
    return _GRN


def status_text(row: dict) -> tuple[str, str]:
    """Return (text, color) for STATUS column."""
    if row["dead"]:
        return "DOWN", _RED
    if not row["state_known"]:
        return "NOSTATE", _RED
    if row["quarantined"]:
        return "QUAR", _RED
    if row["stabilizing"]:
        return "STABLE", _CYN
    if row["reconnecting"]:
        return "RECON", _CYN
    if row["draining"]:
        return "DRAIN", _YEL
    if row["restart_backoff"] > 0:
        return "BACKOFF", _YEL
    if row["restart_scheduled"]:
        return "PEND", _YEL
    if row["blocked"]:
        return "BLOCK", _RED
    if row["cooldown"]:
        return "COOL", _YEL
    if not row["usable"]:
        return "UNUSE", _RED
    if not row["probe_ok"]:
        return "NOIP", _YEL
    if row["active_requests"] > 0:
        return "BUSY", _YEL
    if row["latency_ms"] > 3000:
        return "SLOW", _YEL
    return "OK", _GRN


def effective_usable(ph: dict, has_state: bool = True) -> bool:
    """Fail closed whenever the engine reports an active recovery lifecycle."""
    if not has_state or not bool(ph.get("usable", False)):
        return False
    return not (
        bool(ph.get("draining", False))
        or bool(ph.get("reconnecting", False))
        or bool(ph.get("stabilizing", False))
        or bool(ph.get("quarantined", False))
        or int(ph.get("restart_backoff_for_seconds", 0) or 0) > 0
        or bool(ph.get("restart_scheduled", False))
    )


# --- Rendering ---------------------------------------------------------------

# (header, width)
COLS = [
    ("IDX", 3),
    ("PROVIDER", 10),
    ("STATUS", 7),
    ("TIKTOK IPv4", 15),
    ("IPv6", 32),
    ("LAT(ms)", 8),
    ("ACT", 3),
    ("BLOCK", 6),
    ("COOL", 4),
    ("LAST RESTART MESSAGE", 40),
    ("RX/TX", 13),
]

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def vlen(s: str) -> int:
    """Visible length of a string (ignoring ANSI escape codes)."""
    return len(_ANSI_RE.sub("", s))


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - vlen(s))


def trunc(s: str, width: int) -> str:
    if vlen(s) <= width:
        return s
    # Truncate by visible length
    out, count = [], 0
    for ch in s:
        if count + 1 >= width:
            out.append("…")
            break
        out.append(ch)
        count += 1
    return "".join(out)


def main() -> int:
    # usage: monitor-wireproxy-render.py <cycle-dir> [--count N]
    #        [--check --min-usable N]
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: monitor-wireproxy-render.py <cycle-dir> [--count N]", file=sys.stderr)
        return 2
    cycle_dir = sys.argv[1]
    proxy_count = 18
    check_mode = False
    min_usable = 1
    args = sys.argv[2:]
    pos = 0
    while pos < len(args):
        arg = args[pos]
        if arg == "--count" and pos + 1 < len(args):
            try:
                proxy_count = int(args[pos + 1])
            except ValueError:
                print(f"ERROR: --count must be integer, got {args[pos + 1]!r}", file=sys.stderr)
                return 1
            pos += 2
        elif arg == "--min-usable" and pos + 1 < len(args):
            try:
                min_usable = int(args[pos + 1])
            except ValueError:
                print(f"ERROR: --min-usable must be integer, got {args[pos + 1]!r}", file=sys.stderr)
                return 1
            pos += 2
        elif arg == "--check":
            check_mode = True
            pos += 1
        else:
            print(f"ERROR: unknown or incomplete argument: {arg}", file=sys.stderr)
            return 1
    if not os.path.isdir(cycle_dir):
        print(f"ERROR: not a directory: {cycle_dir}", file=sys.stderr)
        return 1

    # 1. Read gateway health
    health_path = os.path.join(cycle_dir, "health.json")
    try:
        with open(health_path) as f:
            health = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        health = {"status": "down", "proxies": [], "workers": []}

    proxies_path = os.path.join(cycle_dir, "proxies.json")
    try:
        with open(proxies_path) as f:
            proxy_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        proxy_state = {"total": 0, "usable": 0, "blocked": 0, "proxies": []}

    def proxy_container(entry: dict) -> str:
        match = re.search(r"wireproxy-(\d+)", str(entry.get("proxy", "")))
        return f"wireproxy-{int(match.group(1)):02d}" if match else ""

    state_entries = proxy_state.get("proxies", [])
    state_available = not proxy_state.get("monitor_error") and bool(state_entries)
    proxies_health = {
        proxy_container(p): p for p in state_entries if proxy_container(p)
    }

    # 2. Build rows
    rows: list[dict] = []
    for i in range(1, proxy_count + 1):
        idx = f"{i:02d}"
        cid = f"wireproxy-{idx}"
        ph = proxies_health.get(cid, {})

        m = parse_metrics(safe_read(os.path.join(cycle_dir, f"m-{idx}")))
        restart = parse_restart_message(safe_read(os.path.join(cycle_dir, f"restart-{idx}")))
        http_code, latency_s = parse_latency_file(
            safe_read(os.path.join(cycle_dir, f"lat-{idx}"))
        )
        probe_ipv4 = safe_read(os.path.join(cycle_dir, f"ip-{idx}")).strip()
        state_ipv4 = str(ph.get("exit_ip", "") or "").strip()
        exit_ipv4 = state_ipv4 if ":" not in state_ipv4 else probe_ipv4
        if not exit_ipv4 or ":" in exit_ipv4:
            exit_ipv4 = probe_ipv4 if ":" not in probe_ipv4 else ""
        exit_ipv6 = safe_read(os.path.join(cycle_dir, f"ipv6-{idx}")).strip()

        # Determine provider & country
        if 18 <= i:
            country = "Cloudflare WARP"
        elif 13 <= i <= 17:
            country = "Mullvad VPN"
        elif 1 <= i <= 12:
            country = "Surfshark VPN"
        else:
            country = "Wireproxy"

        is_probe_ok = (http_code == 200 and bool(probe_ipv4) and ":" not in probe_ipv4)
        has_state = bool(ph)
        dead = bool(ph.get("dead", False))
        blocked = bool(ph.get("blocked", False))
        cooldown = bool(ph.get("cooldown", False))
        draining = bool(ph.get("draining", False))
        reconnecting = bool(ph.get("reconnecting", False))
        stabilizing = bool(ph.get("stabilizing", False))
        quarantined = bool(ph.get("quarantined", False))
        restart_backoff = int(ph.get("restart_backoff_for_seconds", 0) or 0)
        restart_scheduled = bool(ph.get("restart_scheduled", False))
        # Fail closed if an older/mixed engine response reports usable=true
        # while its lifecycle fields say the proxy is being recovered.
        usable = effective_usable(ph, has_state)

        row = {
            "index": i,
            "container": cid,
            "proxy_id": f"p{i}",
            "country": country,
            # Engine liveness is authoritative and already requires repeated,
            # multi-target failures. This one-shot monitor probe is diagnostic.
            "tunnel_healthy": has_state and not dead,
            "probe_ok": is_probe_ok,
            "state_known": has_state,
            "usable": usable,
            "dead": dead,
            "blocked": blocked,
            "blocked_remaining": int(ph.get("blocked_for_seconds", 0) or 0),
            "exit_ipv4": exit_ipv4,
            "exit_ipv6": exit_ipv6,
            "latency_ms": round(latency_s * 1000, 1) if latency_s else 0.0,
            "http_code": http_code,
            "active_requests": int(ph.get("in_flight", 0) or 0),
            "cooldown": cooldown,
            "draining": draining,
            "reconnecting": reconnecting,
            "stabilizing": stabilizing,
            "quarantined": quarantined,
            "restart_backoff": restart_backoff,
            "restart_scheduled": restart_scheduled,
            "endpoint": m["endpoint"],
            "handshake": m["handshake"],
            "restart_message": restart["message"],
            "restart_success": restart["success"],
            "rx_b": m["rx_b"],
            "tx_b": m["tx_b"],
        }
        rows.append(row)

    render(health, rows, proxy_count=proxy_count, state_available=state_available)
    if check_mode:
        usable_count = sum(1 for row in rows if row["usable"] and row["tunnel_healthy"])
        gateway_ok = str(health.get("status", "")).lower() == "healthy"
        if not gateway_ok or not state_available or usable_count < min_usable:
            print(
                f"CHECK FAILED: gateway_ok={str(gateway_ok).lower()} "
                f"state_available={str(state_available).lower()} "
                f"usable={usable_count} required={min_usable}",
                file=sys.stderr,
            )
            return 1
    return 0


def render(health: dict, rows: list, proxy_count: int = 18,
           state_available: bool = True) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gstatus = str(health.get("status", "unknown"))

    bar = c(_BOLD + _CYN, "═" * 145)
    print(bar)
    print(
        c(_BOLD, "  WIREPROXY MONITOR")
        + c(_DIM, f"  •  {ts}  •  {proxy_count} containers")
    )
    gcol = _GRN if gstatus == "healthy" else (_YEL if gstatus == "degraded" else _RED)
    print(c(_DIM, "  Gateway: ") + c(_BOLD + gcol, gstatus))
    print(bar)

    # Header
    header_cells = [c(_BOLD + _CYN, name.ljust(w)) for name, w in COLS]
    print("  " + "  ".join(header_cells))
    sep = "  ".join(c(_DIM, "─" * w) for _, w in COLS)
    print("  " + sep)

    # Group by country
    by_country: dict[str, list] = {}
    for r in rows:
        by_country.setdefault(r["country"], []).append(r)

    tunnel_healthy_count = 0
    usable_count = 0
    blocked_count = 0
    cooldown_count = 0
    no_ip_count = 0
    slow_count = 0

    country_order = sorted(
        by_country.keys(),
        key=lambda k: (COUNTRY_ORDER.get(k, 99), k),
    )

    for country in country_order:
        crows = sorted(by_country[country], key=lambda r: r["index"])
        print()
        print("  " + c(_BOLD + _MAG, f"[{country}]"))
        for r in crows:
            if r["tunnel_healthy"]:
                tunnel_healthy_count += 1
            if r["usable"] and r["tunnel_healthy"]:
                usable_count += 1
            if r["blocked"]:
                blocked_count += 1
            if r["cooldown"]:
                cooldown_count += 1
            if not r["probe_ok"]:
                no_ip_count += 1
            if r["latency_ms"] > 3000:
                slow_count += 1

            stxt, scol = status_text(r)
            status_cell = c(_BOLD + scol, stxt.ljust(COLS[2][1]))

            # TikTok currently resolves to IPv4; this is the exit used for
            # grouping and retry diversity. IPv6 is diagnostic only.
            if r["exit_ipv4"]:
                ipv4_cell = c(status_palette(r), r["exit_ipv4"])
            else:
                ipv4_cell = c(_DIM + _RED, "(no ip)")
            ipv6_cell = c(_DIM, r["exit_ipv6"] or "-")

            # Latency
            if r["latency_ms"] > 0:
                if r["latency_ms"] < 1500:
                    lat_cell = c(_GRN, f"{r['latency_ms']:.0f}")
                elif r["latency_ms"] < 3000:
                    lat_cell = c(_YEL, f"{r['latency_ms']:.0f}")
                else:
                    lat_cell = c(_RED, f"{r['latency_ms']:.0f}")
            else:
                lat_cell = c(_DIM, "-")

            cool_cell = c(_YEL, "Y") if r["cooldown"] else "-"
            if r["blocked"]:
                remaining = r["blocked_remaining"]
                block_cell = c(_RED, f"{remaining}s" if remaining > 0 else "Y")
            else:
                block_cell = "-"

            # Last restart message
            restart_msg = trunc(r["restart_message"] or "-", COLS[9][1])
            if r["restart_success"] is True:
                restart_cell = c(_GRN, restart_msg)
            elif r["restart_success"] is False:
                restart_cell = c(_RED, restart_msg)
            else:
                restart_cell = c(_DIM, restart_msg)

            # RX/TX
            rx_tx = f"{human_bytes(r['rx_b'])}/{human_bytes(r['tx_b'])}"
            rx_tx_cell = c(_DIM, rx_tx)

            cells = [
                str(r["index"]).ljust(COLS[0][1]),
                r["country"][: COLS[1][1]].ljust(COLS[1][1]),
                status_cell,
                pad(ipv4_cell, COLS[3][1]),
                pad(ipv6_cell, COLS[4][1]),
                pad(lat_cell, COLS[5][1]),
                str(r["active_requests"]).ljust(COLS[6][1]),
                pad(block_cell, COLS[7][1]),
                pad(cool_cell, COLS[8][1]),
                pad(restart_cell, COLS[9][1]),
                pad(rx_tx_cell, COLS[10][1]),
            ]
            print("  " + "  ".join(cells))

    # Footer
    print()
    print(bar)
    unique_ipv4 = len({r["exit_ipv4"] for r in rows if r["exit_ipv4"]})
    usable_unique_ipv4 = len({
        r["exit_ipv4"] for r in rows
        if r["exit_ipv4"] and r["usable"] and r["tunnel_healthy"]
    })
    blocked_unique_ipv4 = len({
        r["exit_ipv4"] for r in rows if r["exit_ipv4"] and r["blocked"]
    })
    unique_ipv6 = len({r["exit_ipv6"] for r in rows if r["exit_ipv6"]})
    parts = [
        c(_GRN, f"{tunnel_healthy_count}/{proxy_count} tunnels healthy"),
        c(_CYN, f"{unique_ipv4} unique TikTok IPv4"),
        c(_CYN, f"{usable_unique_ipv4}/{unique_ipv4} usable IPv4"),
    ]
    if state_available:
        parts.insert(1, c(_GRN if usable_count else _RED,
                          f"{usable_count}/{proxy_count} TikTok usable"))
    else:
        parts.insert(1, c(_RED, "TikTok state unavailable"))
    if unique_ipv6:
        parts.append(c(_DIM, f"{unique_ipv6} IPv6 capable"))
    if blocked_count:
        parts.append(c(_RED, f"{blocked_unique_ipv4} blocked IPv4 ({blocked_count} containers)"))
    if cooldown_count:
        parts.append(c(_YEL, f"{cooldown_count} in cooldown"))
    if no_ip_count:
        parts.append(c(_RED, f"{no_ip_count} no-ip/http-err"))
    if slow_count:
        parts.append(c(_YEL, f"{slow_count} slow (>3s)"))
    print("  " + c(_BOLD, "SUMMARY: ") + "  ".join(parts))
    print(bar)


if __name__ == "__main__":
    sys.exit(main())
