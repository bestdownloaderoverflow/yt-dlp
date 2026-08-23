#!/usr/bin/env bash
# monitor-worker.sh - Realtime monitor untuk tiktok-ssr-engine & 18 Wireproxy Pool
#
# Usage:
#   ./scripts/monitor-worker.sh            # Live watch mode (refresh tiap 5 detik)
#   ./scripts/monitor-worker.sh --once     # Tampilkan 1x snapshot lalu exit
#   ./scripts/monitor-worker.sh --test     # Jalankan live extraction test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${PORT:-9111}"
HOST="http://127.0.0.1:${PORT}"
CONTAINER_NAME="tiktok-ssr-engine"
REDIS_CONTAINER="ytdlp-redis-wireproxy"

# Auto-detect TIKTOK_API_KEY from environment, .env file, or container inspect
if [[ -z "${TIKTOK_API_KEY:-}" ]]; then
  if [[ -f "${PROJECT_DIR}/.env" ]]; then
    TIKTOK_API_KEY=$(grep -E '^(TIKTOK_API_KEY|API_KEY)=' "${PROJECT_DIR}/.env" | head -n1 | cut -d '=' -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true)
  fi
fi
if [[ -z "${TIKTOK_API_KEY:-}" ]]; then
  TIKTOK_API_KEY=$(docker inspect "${CONTAINER_NAME}" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -E '^TIKTOK_API_KEY=' | cut -d '=' -f2- || true)
fi

GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
BOLD="\033[1m"
NC="\033[0m"

run_test() {
  echo -e "${CYAN}=== Menjalankan Live Test Ekstraksi via ${HOST}/tiktok ===${NC}"
  local start_time
  start_time=$(date +%s%N 2>/dev/null || date +%s)
  
  local response
  response=$(curl -s -w "\n%{http_code}" -X POST "${HOST}/tiktok" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${TIKTOK_API_KEY:-}" \
    -d '{"url":"https://www.tiktok.com/@jjtrailwalker/video/7660242147043544334"}' 2>/dev/null || echo -e "{}\n000")
  
  local http_code
  http_code=$(echo "$response" | tail -n1)
  local body
  body=$(echo "$response" | sed '$d')

  if [[ "$http_code" == "200" ]]; then
    local title
    title=$(echo "$body" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('title','-'))" 2>/dev/null || echo "-")
    local src
    src=$(echo "$body" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('extract_source','-'))" 2>/dev/null || echo "-")
    echo -e "${GREEN}✓ Status HTTP 200 OK${NC}"
    echo -e "  Title  : ${BOLD}${title}${NC}"
    echo -e "  Source : ${YELLOW}${src}${NC}"
  else
    echo -e "${RED}✗ Ekstraksi gagal (HTTP ${http_code}): ${body}${NC}"
  fi
}

show_status() {
  clear 2>/dev/null || true
  echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════════════════════════${NC}"
  echo -e "  ${BOLD}🚀 TIKTOK SSR ENGINE & 18 WIREPROXY MONITOR${NC}  •  $(date '+%Y-%m-%d %H:%M:%S')"
  echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════════════════════════${NC}"

  # 1. Container Status & Stats
  echo -e "\n${BOLD}[1] ENGINE STATUS (${CONTAINER_NAME})${NC}"
  if docker ps --filter "name=^/${CONTAINER_NAME}$" --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    local stats
    stats=$(docker stats "${CONTAINER_NAME}" --no-stream --format "CPU: {{.CPUPerc}} | MEM: {{.MemUsage}} ({{.MemPerc}})" 2>/dev/null || echo "N/A")
    local health
    health=$(curl -s "${HOST}/health" 2>/dev/null || echo '{"status":"down"}')
    local uptime
    uptime=$(echo "$health" | python3 -c "import sys, json; print(json.load(sys.stdin).get('uptime_seconds', 0))" 2>/dev/null || echo "0")
    
    echo -e "  Status     : ${GREEN}● RUNNING${NC} (${stats})"
    echo -e "  HTTP Port  : ${BOLD}${PORT}${NC} (${HOST}/health)"
    echo -e "  Uptime     : ${uptime} detik"
  else
    echo -e "  Status     : ${RED}● STOPPED / NOT FOUND${NC}"
  fi

  # 2. Redis Session Status
  echo -e "\n${BOLD}[2] REDIS SESSION CACHE${NC}"
  if docker ps --filter "name=^/${REDIS_CONTAINER}$" --format '{{.Names}}' | grep -qx "${REDIS_CONTAINER}"; then
    local keys_count
    keys_count=$(docker exec "${REDIS_CONTAINER}" redis-cli -n 1 DBSIZE 2>/dev/null | awk '{print $1}' || echo "0")
    echo -e "  Redis DB 1 : ${GREEN}● ACTIVE${NC} (${keys_count} active stream sessions)"
  else
    echo -e "  Redis      : ${YELLOW}● Redis container not detected${NC}"
  fi

  # 3. 18 Wireproxy Pool Summary
  echo -e "\n${BOLD}[3] 18 WIREPROXY POOL STATUS${NC}"
  local running_proxies
  running_proxies=$(docker ps --filter "name=^/wireproxy-" --format '{{.Names}}' | wc -l | tr -d ' ')
  echo -e "  Active Containers: ${BOLD}${running_proxies}/18${NC}"
  
  if [[ "${1:-}" == "--detail" ]] || [[ "${DETAIL:-0}" == "1" ]]; then
    "${SCRIPT_DIR}/monitor-wireproxy.sh" --once --no-color | tail -n +5
  else
    echo -e "  Gunakan ${CYAN}./scripts/monitor-wireproxy.sh${NC} untuk rincian IP & latency masing-masing proxy."
  fi

  echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════════════════════════════════════════════════${NC}"
  echo -e "  Tekan ${BOLD}Ctrl+C${NC} untuk keluar | Mode: ${BOLD}${MODE}${NC}"
}

# --- Arg parsing ---
MODE="watch"
if [[ "${1:-}" == "--test" ]]; then
  run_test
  exit 0
fi

if [[ "${1:-}" == "--once" ]]; then
  MODE="once"
  show_status
  exit 0
fi

while true; do
  show_status
  sleep 5
done
