#!/usr/bin/env bash
set -euo pipefail

# WireGuard account check via temporary Gluetun container.
# You can override any variable below via environment variables.

GLUETUN_IMAGE="${GLUETUN_IMAGE:-qmcgaw/gluetun:v3.41.1}"
CONTROL_PORT="${CONTROL_PORT:-18080}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-90}"
CONTAINER_NAME="${CONTAINER_NAME:-wg-check-$(date +%s)}"

VPN_SERVICE_PROVIDER="${VPN_SERVICE_PROVIDER:-custom}"
VPN_TYPE="${VPN_TYPE:-wireguard}"
WIREGUARD_PRIVATE_KEY="${WIREGUARD_PRIVATE_KEY:-mJNxbqpODxFWrNpoJnNJt3GAZaegIFuiY6XQekl0zkI=}"
WIREGUARD_ADDRESSES="${WIREGUARD_ADDRESSES:-172.16.0.2/32}"
WIREGUARD_PUBLIC_KEY="${WIREGUARD_PUBLIC_KEY:-bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=}"
WIREGUARD_ENDPOINT_IP="${WIREGUARD_ENDPOINT_IP:-162.159.192.1}"
WIREGUARD_ENDPOINT_PORT="${WIREGUARD_ENDPOINT_PORT:-2408}"
DNS_ADDRESS="${DNS_ADDRESS:-1.1.1.1}"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: command '$1' tidak ditemukan"
    exit 2
  fi
}

check_docker_server() {
  local server_os
  if ! docker version >/dev/null 2>&1; then
    echo "ERROR: Docker daemon tidak bisa diakses."
    echo "Coba cek: docker version"
    echo "Jika di Linux VPS: sudo systemctl status docker && sudo systemctl restart docker"
    exit 2
  fi

  server_os="$(docker info --format '{{.OSType}}' 2>/dev/null || true)"
  if [[ -z "${server_os}" ]]; then
    echo "ERROR: Docker server OS tidak terbaca (context/daemon kemungkinan bermasalah)."
    echo "Coba: unset DOCKER_HOST DOCKER_CONTEXT && docker context use default"
    echo "Lalu ulangi: docker info"
    exit 2
  fi

  if [[ "${server_os}" != "linux" ]]; then
    echo "ERROR: Docker server OS '${server_os}' tidak didukung untuk test ini."
    echo "Jalankan script ini di host Linux yang punya /dev/net/tun."
    exit 2
  fi
}

wait_for_control() {
  local deadline now
  deadline=$((SECONDS + STARTUP_TIMEOUT))
  while true; do
    if curl -fsS "http://127.0.0.1:${CONTROL_PORT}/v1/publicip/ip" >/dev/null 2>&1; then
      return 0
    fi
    now=$SECONDS
    if (( now >= deadline )); then
      return 1
    fi
    sleep 2
  done
}

require_cmd docker
require_cmd curl
check_docker_server

echo "Menjalankan container test: ${CONTAINER_NAME}"
docker run -d --rm \
  --name "$CONTAINER_NAME" \
  --cap-add NET_ADMIN \
  --device /dev/net/tun:/dev/net/tun \
  -p "127.0.0.1:${CONTROL_PORT}:8000" \
  -e VPN_SERVICE_PROVIDER="$VPN_SERVICE_PROVIDER" \
  -e VPN_TYPE="$VPN_TYPE" \
  -e WIREGUARD_PRIVATE_KEY="$WIREGUARD_PRIVATE_KEY" \
  -e WIREGUARD_ADDRESSES="$WIREGUARD_ADDRESSES" \
  -e WIREGUARD_PUBLIC_KEY="$WIREGUARD_PUBLIC_KEY" \
  -e WIREGUARD_ENDPOINT_IP="$WIREGUARD_ENDPOINT_IP" \
  -e WIREGUARD_ENDPOINT_PORT="$WIREGUARD_ENDPOINT_PORT" \
  -e DNS_ADDRESS="$DNS_ADDRESS" \
  -e HTTP_CONTROL_SERVER_ADDRESS=":8000" \
  -e HTTP_CONTROL_SERVER_LOG="on" \
  "$GLUETUN_IMAGE" >/dev/null

echo "Menunggu tunnel up (timeout ${STARTUP_TIMEOUT}s)..."
if ! wait_for_control; then
  echo "FAIL: Gluetun tidak ready dalam ${STARTUP_TIMEOUT} detik"
  echo "--- Logs (tail 80) ---"
  docker logs --tail 80 "$CONTAINER_NAME" || true
  exit 1
fi

if ! docker exec "$CONTAINER_NAME" sh -c "ip link show wg0 >/dev/null 2>&1"; then
  echo "FAIL: interface wg0 tidak ditemukan"
  echo "--- Logs (tail 80) ---"
  docker logs --tail 80 "$CONTAINER_NAME" || true
  exit 1
fi

PUBLIC_IP="$(curl -fsS "http://127.0.0.1:${CONTROL_PORT}/v1/publicip/ip" | tr -d '\r\n' || true)"
if [[ -z "$PUBLIC_IP" ]]; then
  echo "FAIL: tidak bisa mendapatkan public IP dari Gluetun"
  echo "--- Logs (tail 80) ---"
  docker logs --tail 80 "$CONTAINER_NAME" || true
  exit 1
fi

echo "PASS: WireGuard account/config terhubung"
echo "Public IP: ${PUBLIC_IP}"
echo "--- Logs (tail 30) ---"
docker logs --tail 30 "$CONTAINER_NAME" || true
