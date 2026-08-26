#!/usr/bin/env bash
# monitor-wireproxy.sh - monitor tunnel dan state rotasi TikTok wireproxy.
#
# Menggabungkan 4 sumber data:
#   1. GET http://localhost:9111/health              -> health service
#      GET /proxies dari dalam container             -> state rotasi TikTok
#   2. docker exec wireproxy-XX wget /metrics         -> WireGuard handshake + transfer
#   3. docker exec probe-container curl --proxy       -> IPv4 TikTok-relevant,
#      IPv6 (WARP only), dan latency per container
#   4. docker exec ytdlp-redis-wireproxy redis-cli    -> last restart message
#
# Tabel dirender dengan python helper (monitor-wireproxy-render.py) berwarna,
# dikelompokkan per negara, refresh-able.
#
# Usage:
#   ./monitor-wireproxy.sh                 # watch mode, refresh 5 detik
#   ./monitor-wireproxy.sh --once          # snapshot 1x lalu exit
#   ./monitor-wireproxy.sh --check         # snapshot + exit nonzero jika threshold gagal
#   ./monitor-wireproxy.sh --check --min-usable 10
#   ./monitor-wireproxy.sh --interval 3    # watch mode, refresh 3 detik
#   ./monitor-wireproxy.sh --no-color      # nonaktifkan ANSI (untuk pipe ke log)
#   ./monitor-wireproxy.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RENDER_PY="${SCRIPT_DIR}/monitor-wireproxy-render.py"

if [[ -z "${GATEWAY_CONTAINER:-}" ]]; then
  if docker ps --filter "name=^/tiktok-ssr-engine$" --format '{{.Names}}' | grep -qx "tiktok-ssr-engine"; then
    GATEWAY_CONTAINER="tiktok-ssr-engine"
    SOCKS_PROBE_CONTAINER="${SOCKS_PROBE_CONTAINER:-tiktok-ssr-engine}"
  else
    GATEWAY_CONTAINER="ytdlp-gateway-wireproxy"
    SOCKS_PROBE_CONTAINER="${SOCKS_PROBE_CONTAINER:-ytdlp-worker-1}"
  fi
else
  SOCKS_PROBE_CONTAINER="${SOCKS_PROBE_CONTAINER:-${GATEWAY_CONTAINER}}"
fi
REDIS_CONTAINER="${REDIS_CONTAINER:-ytdlp-redis-wireproxy}"
GATEWAY_PORT="${GATEWAY_PORT:-9111}"
DETECTED_COUNT=$(docker ps --filter "name=^/wireproxy-" --format '{{.Names}}' 2>/dev/null | wc -l | tr -d ' ' || echo "0")
if [[ "${DETECTED_COUNT}" -gt 0 ]]; then
  PROXY_COUNT="${PROXY_COUNT:-${DETECTED_COUNT}}"
else
  PROXY_COUNT="${PROXY_COUNT:-50}"
fi
IPV4_TRACE_URL="${IPV4_TRACE_URL:-https://1.1.1.1/cdn-cgi/trace}"
IPV4_CHECK_URL="${IPV4_CHECK_URL:-https://api.ipify.org}"
IPV6_CHECK_URL="${IPV6_CHECK_URL:-https://api64.ipify.org}"
WARP_FIRST_INDEX="${WARP_FIRST_INDEX:-18}"
MIN_USABLE="${MIN_USABLE:-1}"

INTERVAL=5
MODE="watch"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

# --- arg parsing -------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)        MODE="once"; shift ;;
    --check)       MODE="check"; shift ;;
    --min-usable)  MIN_USABLE="${2:-}"; shift 2 ;;
    --min-usable=*) MIN_USABLE="${1#*=}"; shift ;;
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
if ! [[ "$MIN_USABLE" =~ ^[0-9]+$ ]] || [[ "$MIN_USABLE" -lt 1 ]]; then
  echo "ERROR: --min-usable harus integer positif" >&2
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

REDIS_AVAILABLE=0
if docker ps --filter "name=^/${REDIS_CONTAINER}$" --format '{{.Names}}' \
     | grep -qx "${REDIS_CONTAINER}"; then
  REDIS_AVAILABLE=1
fi

# --- per-iteration work ------------------------------------------------------

CURRENT_TMPDIR=""

cleanup() {
  local rc=$?
  local pids
  pids="$(jobs -p 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill $pids 2>/dev/null || true
    wait $pids 2>/dev/null || true
  fi
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

  [[ -d "$dir" ]] || return 0

  # Metrics: wg show style text dari :9080/ (wireproxy info HTML page), stripped by renderer
  docker exec "$c" wget -qO- --timeout=5 --tries=1 \
    http://127.0.0.1:9080/ > "${dir}/m-${i}" 2>/dev/null || true

  # SOCKS5 probe: keluar lewat ${c}:1080, dilihat dari ytdlp-worker-1.
  # Jangan pakai curl -o "${dir}/ip-${i}" di dalam docker exec: path itu
  # akan dianggap path di container worker, bukan temp dir host monitor.
  local probe_out status_line body trace_ip ipv6_out ipv6_status ipv6_body
  # Primary: Cloudflare trace is fast and returns the effective IPv4 as `ip=`.
  # Keep ipify as an independent fallback so one provider cannot create NOIP
  # noise for an otherwise healthy tunnel.
  probe_out="$(
    docker exec "${SOCKS_PROBE_CONTAINER}" curl --proxy "socks5h://${c}:1080" \
      -m 5 -sS \
      -w $'\n%{http_code} %{time_total}\n' \
      "${IPV4_TRACE_URL}" 2>/dev/null || true
  )"
  status_line="$(printf '%s\n' "$probe_out" | tail -n 1)"
  body="$(printf '%s\n' "$probe_out" | sed '$d')"
  trace_ip="$(printf '%s\n' "$body" | sed -n 's/^ip=//p' | head -n 1 | tr -d '\r')"

  if [[ "$status_line" == 200\ * && "$trace_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    body="$trace_ip"
  else
    probe_out="$(
      docker exec "${SOCKS_PROBE_CONTAINER}" curl --proxy "socks5h://${c}:1080" \
        -m 8 -sS \
        -w $'\n%{http_code} %{time_total}\n' \
        "${IPV4_CHECK_URL}" 2>/dev/null || true
    )"
    status_line="$(printf '%s\n' "$probe_out" | tail -n 1)"
    body="$(printf '%s\n' "$probe_out" | sed '$d')"
  fi

  if [[ -d "$dir" ]]; then
    printf '%s' "$body" > "${dir}/ip-${i}" 2>/dev/null || true
    printf '%s\n' "$status_line" > "${dir}/lat-${i}" 2>/dev/null || true
  fi

  # api64 memilih IPv6 ketika tunnel dan destination mendukungnya. Probe ini
  # hanya informasi kapabilitas WARP; selector TikTok tetap memakai IPv4.
  if [[ "$((10#$i))" -ge "$WARP_FIRST_INDEX" ]]; then
    ipv6_out="$(
      docker exec "${SOCKS_PROBE_CONTAINER}" curl --proxy "socks5h://${c}:1080" \
        -m 8 -sS \
        -w $'\n%{http_code}\n' \
        "${IPV6_CHECK_URL}" 2>/dev/null || true
    )"
    ipv6_status="$(printf '%s\n' "$ipv6_out" | tail -n 1)"
    ipv6_body="$(printf '%s\n' "$ipv6_out" | sed '$d')"
    if [[ "$ipv6_status" == "200" && "$ipv6_body" == *:* && -d "$dir" ]]; then
      printf '%s' "$ipv6_body" > "${dir}/ipv6-${i}" 2>/dev/null || true
    fi
  fi

  if [[ "$REDIS_AVAILABLE" -eq 1 && -d "$dir" ]]; then
    docker exec "${REDIS_CONTAINER}" redis-cli --raw \
      lindex "restart_log:p$((10#$i))" 0 > "${dir}/restart-${i}" 2>/dev/null || true
  fi
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

  # /proxies is authenticated. Resolve the key only inside the gateway
  # container so it never appears in the host process list or monitor output.
  if ! docker exec "${GATEWAY_CONTAINER}" sh -c \
       'curl -fsS --max-time 3 -H "X-API-Key: ${TIKTOK_API_KEY:-}" http://127.0.0.1:9111/proxies' \
       > "${dir}/proxies.json" 2>/dev/null; then
    echo '{"monitor_error":"proxies endpoint unavailable","total":0,"usable":0,"blocked":0,"proxies":[]}' \
      > "${dir}/proxies.json"
  fi

  # 2. Fan out all per-container probes in parallel.
  local pids=()
  for i in $(seq -w 1 "$PROXY_COUNT"); do
    probe_one_container "$i" "$dir" &
    pids+=("$!")
  done
  for p in "${pids[@]}"; do
    wait "$p" 2>/dev/null || true
  done

  # 3. Render
  if [[ "$MODE" == "watch" ]]; then
    clear
  fi
  local render_args=("$dir" --count "$PROXY_COUNT")
  if [[ "$MODE" == "check" ]]; then
    render_args+=(--check --min-usable "$MIN_USABLE")
  fi
  python3 "$RENDER_PY" "${render_args[@]}"

  # 4. Cleanup temp dir (avoid accumulation in watch mode)
  rm -rf "$dir"
  CURRENT_TMPDIR=""
}

# --- main loop ---------------------------------------------------------------

if [[ "$MODE" == "once" || "$MODE" == "check" ]]; then
  do_iteration
  exit 0
fi

# watch mode
while true; do
  do_iteration
  sleep "$INTERVAL"
done
