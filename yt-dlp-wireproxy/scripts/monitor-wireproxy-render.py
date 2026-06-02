#!/usr/bin/env python3
"""monitor-wireproxy-render.py - render detail table for 18 wireproxy containers.

Reads from a per-cycle temp directory:
  health.json   - output of GET http://localhost:9111/health
  m-XX          - text/wg-show output from wireproxy-XX /metrics
  ip-XX         - exit IP captured from SOCKS5 probe
  lat-XX        - "<http_code> <time_total>" captured from SOCKS5 probe

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

RE_ENDPOINT = re.compile(r"endpoint:\s+(\S+)")
RE_HANDSHAKE = re.compile(r"latest handshake:\s+(.+)")
RE_TRANSFER = re.compile(
    r"transfer:\s+([\d.]+)\s*(\w+)\s*received,\s+([\d.]+)\s*(\w+)\s*sent"
)

UNIT_FACTOR = {
    "b": 1, "byte": 1, "bytes": 1,
    "kib": 1024, "kb": 1000, "k": 1000, "ki": 1024,
    "mib": 1024 ** 2, "mb": 1000 ** 2, "m": 1000 ** 2, "mi": 1024 ** 2,
    "gib": 1024 ** 3, "gb": 1000 ** 3, "g": 1000 ** 3, "gi": 1024 ** 3,
    "tib": 1024 ** 4, "tb": 1000 ** 4,
}

COUNTRY_ORDER = {"Indonesia": 0, "Singapore": 1, "Vietnam": 2, "WARP": 3}


# --- Parsers -----------------------------------------------------------------

def parse_metrics(text: str) -> dict:
    out = {"endpoint": "", "handshake": "", "rx_b": 0, "tx_b": 0}
    if not text:
        return out
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


# --- Handshake color ---------------------------------------------------------

def _handshake_age_seconds(handshake: str) -> int | None:
    """Parse 'X seconds/minutes/hours ago' or 'never' into seconds.

    Returns None if unparseable.
    """
    if not handshake or handshake == "never":
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
    if handshake == "never":
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
    if row["restarting"]:
        return _RED
    if not row["healthy"]:
        return _RED
    if row["cooldown"]:
        return _YEL
    if row["active_requests"] > 0:
        return _YEL
    if not row["exit_ip"] or row["http_code"] != 200:
        return _RED
    if row["failures"] > 0:
        return _YEL
    if row["latency_ms"] > 3000:
        return _YEL
    if row["latency_ms"] > 1500:
        return _YEL
    return _GRN


def status_text(row: dict) -> tuple[str, str]:
    """Return (text, color) for STATUS column."""
    if row["restarting"]:
        return "RESTART", _RED
    if not row["healthy"]:
        return "DOWN", _RED
    if row["cooldown"]:
        return "COOL", _YEL
    if not row["exit_ip"] or row["http_code"] != 200:
        return "NOIP", _YEL
    if row["active_requests"] > 0:
        return "BUSY", _YEL
    if row["latency_ms"] > 3000:
        return "SLOW", _YEL
    return "OK", _GRN


# --- Rendering ---------------------------------------------------------------

# (header, width)
COLS = [
    ("IDX", 3),
    ("CN", 10),
    ("STATUS", 7),
    ("EXIT IP", 15),
    ("LAT(ms)", 8),
    ("ACT", 3),
    ("FAIL", 4),
    ("COOL", 4),
    ("RST", 3),
    ("HANDSHAKE", 16),
    ("ENDPOINT", 32),
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
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: monitor-wireproxy-render.py <cycle-dir> [--count N]", file=sys.stderr)
        return 2
    cycle_dir = sys.argv[1]
    proxy_count = 18
    if len(sys.argv) >= 4 and sys.argv[2] == "--count":
        try:
            proxy_count = int(sys.argv[3])
        except ValueError:
            print(f"ERROR: --count must be integer, got {sys.argv[3]!r}", file=sys.stderr)
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

    proxies_health = {p.get("id", ""): p for p in health.get("proxies", [])}
    workers = health.get("workers", [])

    # 2. Build rows
    rows: list[dict] = []
    for i in range(1, proxy_count + 1):
        idx = f"{i:02d}"
        cid = f"wireproxy-{idx}"
        pid = f"p{i}"
        ph = proxies_health.get(pid, {})

        m = parse_metrics(safe_read(os.path.join(cycle_dir, f"m-{idx}")))
        http_code, latency_s = parse_latency_file(
            safe_read(os.path.join(cycle_dir, f"lat-{idx}"))
        )
        exit_ip = safe_read(os.path.join(cycle_dir, f"ip-{idx}")).strip()

        row = {
            "index": i,
            "container": cid,
            "proxy_id": pid,
            "country": ph.get("country", "?"),
            "healthy": bool(ph.get("healthy", False)),
            "exit_ip": exit_ip,
            "latency_ms": round(latency_s * 1000, 1) if latency_s else 0.0,
            "http_code": http_code,
            "active_requests": int(ph.get("active_requests", 0) or 0),
            "failures": int(ph.get("failures", 0) or 0),
            "cooldown": bool(ph.get("cooldown", False)),
            "restarting": bool(ph.get("restarting", False)),
            "cooldown_remaining": int(ph.get("cooldown_remaining", 0) or 0),
            "endpoint": m["endpoint"],
            "handshake": m["handshake"],
            "rx_b": m["rx_b"],
            "tx_b": m["tx_b"],
        }
        rows.append(row)

    render(health, rows, workers, proxy_count=proxy_count)
    return 0


def render(health: dict, rows: list, workers: list, proxy_count: int = 18) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gstatus = str(health.get("status", "unknown"))

    bar = c(_BOLD + _CYN, "═" * 130)
    print(bar)
    print(
        c(_BOLD, "  WIREPROXY MONITOR")
        + c(_DIM, f"  •  {ts}  •  {proxy_count} containers")
    )
    gcol = _GRN if gstatus == "healthy" else (_YEL if gstatus == "degraded" else _RED)
    print(c(_DIM, "  Gateway: ") + c(_BOLD + gcol, gstatus))
    if workers:
        ws = sum(1 for w in workers if w.get("healthy"))
        wt = len(workers)
        wcol = _GRN if ws == wt else _YEL
        print(
            c(_DIM, "  Workers: ")
            + c(_BOLD + wcol, f"{ws}/{wt} healthy")
        )
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

    healthy_count = 0
    cooldown_count = 0
    restarting_count = 0
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
            if r["healthy"]:
                healthy_count += 1
            if r["cooldown"]:
                cooldown_count += 1
            if r["restarting"]:
                restarting_count += 1
            if not r["exit_ip"] or r["http_code"] != 200:
                no_ip_count += 1
            if r["latency_ms"] > 3000:
                slow_count += 1

            stxt, scol = status_text(r)
            status_cell = c(_BOLD + scol, stxt.ljust(COLS[2][1]))

            # Exit IP
            if r["exit_ip"]:
                ip_cell = c(status_palette(r), r["exit_ip"])
            else:
                ip_cell = c(_DIM + _RED, "(no ip)")

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

            # Cooldown remaining
            if r["cooldown"] and r["cooldown_remaining"] > 0:
                cool_cell = c(_YEL, f"{r['cooldown_remaining']}s")
            else:
                cool_cell = "-"

            # Restart flag
            if r["restarting"]:
                rst_cell = c(_RED, "Y")
            else:
                rst_cell = c(_DIM, "-")

            # Handshake
            hs = r["handshake"] or "-"
            hs_cell = c(handshake_color(hs), hs)

            # Endpoint
            ep = r["endpoint"] or "-"
            ep_cell = ep if vlen(ep) <= COLS[10][1] else trunc(ep, COLS[10][1])

            # RX/TX
            rx_tx = f"{human_bytes(r['rx_b'])}/{human_bytes(r['tx_b'])}"
            rx_tx_cell = c(_DIM, rx_tx)

            # Failures (red if > 0)
            if r["failures"] > 0:
                fail_cell = c(_RED, str(r["failures"]))
            else:
                fail_cell = c(_DIM, "0")

            cells = [
                str(r["index"]).ljust(COLS[0][1]),
                r["country"][: COLS[1][1]].ljust(COLS[1][1]),
                status_cell,
                pad(ip_cell, COLS[3][1]),
                pad(lat_cell, COLS[4][1]),
                str(r["active_requests"]).ljust(COLS[5][1]),
                pad(fail_cell, COLS[6][1]),
                pad(cool_cell, COLS[7][1]),
                rst_cell.ljust(COLS[8][1]),
                pad(hs_cell, COLS[9][1]),
                pad(ep_cell, COLS[10][1]),
                pad(rx_tx_cell, COLS[11][1]),
            ]
            print("  " + "  ".join(cells))

    # Footer
    print()
    print(bar)
    parts = [
        c(_GRN, f"{healthy_count}/{proxy_count} healthy"),
    ]
    if cooldown_count:
        parts.append(c(_YEL, f"{cooldown_count} in cooldown"))
    if restarting_count:
        parts.append(c(_RED, f"{restarting_count} restarting"))
    if no_ip_count:
        parts.append(c(_RED, f"{no_ip_count} no-ip/http-err"))
    if slow_count:
        parts.append(c(_YEL, f"{slow_count} slow (>3s)"))
    print("  " + c(_BOLD, "SUMMARY: ") + "  ".join(parts))
    print(bar)


if __name__ == "__main__":
    sys.exit(main())
