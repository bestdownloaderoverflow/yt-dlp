#!/usr/bin/env bash
# restart-dead-proxies.sh - restart wireproxy containers whose SOCKS5 tunnel is dead.
#
# Engine-side prober (tiktok-ssr-engine) only MARKS dead proxies; this script is
# the self-healing counterpart that actually RESTARTS them (needs Docker access).
#
# Usage:
#   ./scripts/restart-dead-proxies.sh             # probe + restart dead tunnels
#   ./scripts/restart-dead-proxies.sh --dry-run   # only report, no restart
#
# Cron (host):
#   */5 * * * * /path/to/yt-dlp-wireproxy/scripts/restart-dead-proxies.sh >> /tmp/wireproxy-restart.log 2>&1
#
# Env overrides:
#   ENGINE_CONTAINER         container used to run the SOCKS5 probe (default: tiktok-ssr-engine)
#   RESTART_COOLDOWN_SECONDS min seconds between restarts of the same container (default: 300)

set -uo pipefail

ENGINE_CONTAINER="${ENGINE_CONTAINER:-tiktok-ssr-engine}"
RESTART_COOLDOWN_SECONDS="${RESTART_COOLDOWN_SECONDS:-300}"
RESTART_COOLDOWN_FILE="${RESTART_COOLDOWN_FILE:-/tmp/wireproxy-restart-cooldown}"

dry=0
[[ "${1:-}" == "--dry-run" ]] && dry=1

if ! docker ps --format '{{.Names}}' | grep -qx "$ENGINE_CONTAINER"; then
  echo "ERROR: probe container '$ENGINE_CONTAINER' is not running" >&2
  exit 1
fi

probe() {
  local container="$1"
  docker exec "$ENGINE_CONTAINER" python3 -c "
import asyncio, sys
from curl_cffi.requests import AsyncSession

async def main():
    url = 'socks5h://$container:1080'
    try:
        async with AsyncSession(proxies={'http': url, 'https': url}, timeout=6) as s:
            r = await s.get('https://1.1.1.1/cdn-cgi/trace', timeout=6)
            sys.exit(0 if r.status_code == 200 else 1)
    except Exception:
        sys.exit(1)

asyncio.run(main())
" >/dev/null 2>&1
}

restarted=0
skipped=0
now=$(date +%s)

for c in $(docker ps --format '{{.Names}}' | grep -E '^wireproxy-[0-9]+$' | sort); do
  if probe "$c"; then
    continue
  fi

  # One failed probe can be a transient blip; require two consecutive failures.
  sleep 3
  if probe "$c"; then
    continue
  fi

  last=0
  if [ -f "$RESTART_COOLDOWN_FILE" ]; then
    last=$(awk -v k="$c" '$1==k{print $2}' "$RESTART_COOLDOWN_FILE")
    last=${last:-0}
  fi
  if (( now - last < RESTART_COOLDOWN_SECONDS )); then
    echo "[skip] $c tunnel dead (2/2) but restart cooldown active ($((RESTART_COOLDOWN_SECONDS - (now - last)))s left)"
    skipped=$((skipped+1))
    continue
  fi

  echo "[restart] $c tunnel dead (2/2 probes) -> docker restart"
  if (( dry == 0 )); then
    docker restart --time 20 "$c" >/dev/null
    echo "$c $now" >> "$RESTART_COOLDOWN_FILE"
  fi
  restarted=$((restarted+1))
done

echo "done: restarted=$restarted skipped_cooldown=$skipped"
