#!/usr/bin/env bash
# monitor-wireproxy.sh - detail monitor untuk 18 wireproxy container.
#
# Menggabungkan 3 sumber data:
#   1. GET http://localhost:9111/health              -> state aggregate dari gateway
#   2. docker exec wireproxy-XX wget /metrics         -> WireGuard handshake + transfer
#   3. docker exec ytdlp-worker-1 curl --proxy        -> exit IP + latency per container
#      socks5h://wireproxy-XX:1080 https://api.ipify.org
#
# Tabel dirender dengan python helper (monitor-wireproxy-render.py) berwarna,
# dikelompokkan per negara, refresh-able.
#
# Usage:
#   ./monitor-wireproxy.sh                 # watch mode, refresh 5 detik
#   ./monitor-wireproxy.sh --once          # snapshot 1x lalu exit
#   ./monitor-wireproxy.sh --interval 3    # watch mode, refresh 3 detik
#   ./monitor-wireproxy.sh --no-color      # nonaktifkan ANSI (untuk pipe ke log)
#   ./monitor-wireproxy.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RENDER_PY="${SCRIPT_DIR}/monitor-wireproxy-render.py"

GATEWAY_CONTAINER="${GATEWAY_CONTAINER:-ytdlp-gateway-wireproxy}"
SOCKS_PROBE_CONTAINER="${SOCKS_PROBE_CONTAINER:-ytdlp-worker-1}"
GATEWAY_PORT="${GATEWAY_PORT:-9111}"
PROXY_COUNT="${PROXY_COUNT:-18}"
IP_CHECK_URL="${IP_CHECK_URL:-https://api.ipify.org}"

INTERVAL=5
MODE="watch"

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
}

# --- arg parsing -------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)        MODE="once"; shift ;;
    --interval)    INTERVAL="${2:-}"; shift 2 ;;
    --interval=*)  INTERVAL="${1#*=}"; shift ;;
    --no-color)    export MON_NO_COLOR=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "ERROR: argumen tidak dikenal: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [[ "$INTERVAL" -lt 1 ]]; then
  echo "ERROR: --interval harus integer positif" >&2
  exit 1
fi

# --- pre-flight checks -------------------------------------------------------

for cmd in docker curl python3 awk; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: perintah '$cmd' tidak ditemukan di PATH" >&2
    exit 1
  fi
done

if [[ ! -f "$RENDER_PY" ]]; then
  echo "ERROR: helper render tidak ditemukan: $RENDER_PY" >&2
  exit 1
fi

if ! docker ps --filter "name=^/${GATEWAY_CONTAINER}$" --format '{{.Names}}' \
     | grep -qx "${GATEWAY_CONTAINER}"; then
  echo "ERROR: container ${GATEWAY_CONTAINER} tidak running" >&2
  echo "       jalankan: docker compose -f ${PROJECT_DIR}/docker-compose.wireproxy.yml up -d" >&2
  exit 1
fi

if ! docker ps --filter "name=^/${SOCKS_PROBE_CONTAINER}$" --format '{{.Names}}' \
     | grep -qx "${SOCKS_PROBE_CONTAINER}"; then
  echo "ERROR: container ${SOCKS_PROBE_CONTAINER} (dipakai untuk probe SOCKS5) tidak running" >&2
  echo "       set SOCKS_PROBE_CONTAINER= ke container lain yang punya curl & 1 network" >&2
  exit 1
fi

if ! curl -sS --max-time 3 "http://localhost:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
  echo "ERROR: gateway http://localhost:${GATEWAY_PORT}/health tidak merespons" >&2
  exit 1
fi

# --- per-iteration work ------------------------------------------------------

CURRENT_TMPDIR=""

cleanup() {
  local rc=$?
  if [[ -n "$CURRENT_TMPDIR" && -d "$CURRENT_TMPDIR" ]]; then
    rm -rf "$CURRENT_TMPDIR"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'echo; echo "[interrupted]"; exit 130' INT TERM

probe_one_container() {
  local i="$1"          # "01".."18"
  local c="wireproxy-${i}"
  local dir="$2"

  # Metrics: wg show style text dari :9080/ (wireproxy info HTML page), stripped by renderer
  docker exec "$c" wget -qO- --timeout=5 --tries=1 \
    http://127.0.0.1:9080/ > "${dir}/m-${i}" 2>/dev/null || true

  # SOCKS5 probe: keluar lewat ${c}:1080, dilihat dari ytdlp-worker-1
  docker exec "${SOCKS_PROBE_CONTAINER}" curl --proxy "socks5h://${c}:1080" \
    -m 8 -sS -o "${dir}/ip-${i}" \
    -w '%{http_code} %{time_total}\n' \
    "${IP_CHECK_URL}" > "${dir}/lat-${i}" 2>/dev/null || true
}

do_iteration() {
  local dir
  dir="$(mktemp -d -t mon-wp-XXXXXX)"
  CURRENT_TMPDIR="$dir"

  # 1. Health aggregate
  if ! curl -sS --max-time 3 "http://localhost:${GATEWAY_PORT}/health" \
       > "${dir}/health.json" 2>/dev/null; then
    echo '{"status":"unreachable","proxies":[],"workers":[]}' \
      > "${dir}/health.json"
  fi

  # 2. Fan-out paralel: 18 metrics + 18 socks5 = 36 probe paralel
  local pids=()
  for i in $(seq -w 1 "$PROXY_COUNT"); do
    probe_one_container "$i" "$dir" &
    pids+=("$!")
  done
  # Tunggu semua dengan toleransi individual (1 sudah max-time 8, no need for wait timeout)
  for p in "${pids[@]}"; do
    wait "$p" 2>/dev/null || true
  done

  # 3. Render
  python3 "$RENDER_PY" "$dir" --count "$PROXY_COUNT"

  # 4. Cleanup temp dir (avoid accumulation in watch mode)
  rm -rf "$dir"
  CURRENT_TMPDIR=""
}

# --- main loop ---------------------------------------------------------------

if [[ "$MODE" == "once" ]]; then
  do_iteration
  exit 0
fi

# watch mode
while true; do
  clear
  do_iteration
  sleep "$INTERVAL"
done
