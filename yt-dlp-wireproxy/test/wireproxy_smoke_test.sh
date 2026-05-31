#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONFIG_PATH="${PROJECT_DIR}/gluetun/custom1/wg0.conf"
WIREPROXY_BIN="${WIREPROXY_BIN:-}"
WIREPROXY_VERSION="${WIREPROXY_VERSION:-v1.1.2}"
SOCKS_HOST="${SOCKS_HOST:-127.0.0.1}"
SOCKS_PORT="${SOCKS_PORT:-10880}"
IP_CHECK_URL="${IP_CHECK_URL:-https://api.ipify.org}"
HTTPS_CHECK_URL="${HTTPS_CHECK_URL:-https://example.com}"

TEMP_DIR=""
WIREPROXY_PID=""

usage() {
  cat <<'EOF'
Usage:
  ./test/wireproxy_smoke_test.sh [options]

Options:
  --config PATH         WireGuard config file to test.
                        Default: gluetun/custom1/wg0.conf
  --wireproxy-bin PATH  Existing wireproxy binary. If omitted, use PATH or
                        build a temporary binary with Go.
  --port PORT           Temporary local SOCKS5 port. Default: 10880
  --help                Show this help.

Environment variables:
  WIREPROXY_VERSION     Version installed temporarily when needed. Default: v1.1.2
  IP_CHECK_URL          URL returning the caller IP. Default: https://api.ipify.org
  HTTPS_CHECK_URL       HTTPS URL used for the connectivity check.
EOF
}

cleanup() {
  if [[ -n "${WIREPROXY_PID}" ]] && kill -0 "${WIREPROXY_PID}" 2>/dev/null; then
    kill "${WIREPROXY_PID}" 2>/dev/null || true
    wait "${WIREPROXY_PID}" 2>/dev/null || true
  fi
  if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
    rm -rf "${TEMP_DIR}"
  fi
}

on_exit() {
  status="$?"
  if [[ "${status}" -ne 0 && -n "${TEMP_DIR}" && -f "${TEMP_DIR}/wireproxy.log" ]]; then
    printf '\n--- wireproxy log (last 80 lines) ---\n' >&2
    tail -n 80 "${TEMP_DIR}/wireproxy.log" >&2
  fi
  cleanup
  exit "${status}"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

retry_curl() {
  attempts="$1"
  shift

  attempt=1
  while [[ "${attempt}" -le "${attempts}" ]]; do
    if curl "$@"; then
      return 0
    fi
    if [[ "${attempt}" -lt "${attempts}" ]]; then
      printf 'Request failed; retrying (%s/%s)...\n' "${attempt}" "${attempts}" >&2
      sleep 2
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || fail "--config requires a path"
      CONFIG_PATH="$2"
      shift 2
      ;;
    --wireproxy-bin)
      [[ $# -ge 2 ]] || fail "--wireproxy-bin requires a path"
      WIREPROXY_BIN="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || fail "--port requires a value"
      SOCKS_PORT="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

trap on_exit EXIT
trap 'exit 130' INT TERM

[[ -f "${CONFIG_PATH}" ]] || fail "WireGuard config not found: ${CONFIG_PATH}"
[[ "${SOCKS_PORT}" =~ ^[0-9]+$ ]] || fail "invalid SOCKS5 port: ${SOCKS_PORT}"
command -v curl >/dev/null 2>&1 || fail "curl is required"

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wireproxy-smoke.XXXXXX")"
chmod 700 "${TEMP_DIR}"

if [[ -z "${WIREPROXY_BIN}" ]]; then
  if command -v wireproxy >/dev/null 2>&1; then
    WIREPROXY_BIN="$(command -v wireproxy)"
  else
    command -v go >/dev/null 2>&1 || fail "wireproxy is missing and Go is not installed"
    printf 'wireproxy binary not found; building %s temporarily...\n' "${WIREPROXY_VERSION}"
    mkdir -p "${TEMP_DIR}/bin"
    GOBIN="${TEMP_DIR}/bin" go install "github.com/windtf/wireproxy/cmd/wireproxy@${WIREPROXY_VERSION}"
    WIREPROXY_BIN="${TEMP_DIR}/bin/wireproxy"
  fi
fi

[[ -x "${WIREPROXY_BIN}" ]] || fail "wireproxy binary is not executable: ${WIREPROXY_BIN}"

TEST_CONFIG="${TEMP_DIR}/wireproxy.conf"
cp "${CONFIG_PATH}" "${TEST_CONFIG}"
chmod 600 "${TEST_CONFIG}"

if grep -qE '^\[(Socks5|Http)\]' "${TEST_CONFIG}"; then
  fail "config already contains a proxy listener section; use a plain WireGuard config"
fi

cat >> "${TEST_CONFIG}" <<EOF

[Socks5]
BindAddress = ${SOCKS_HOST}:${SOCKS_PORT}
EOF

printf 'Checking direct IP...\n'
DIRECT_IP="$(curl -4fsS --max-time 15 "${IP_CHECK_URL}")"

printf 'Starting wireproxy SOCKS5 listener on %s:%s...\n' "${SOCKS_HOST}" "${SOCKS_PORT}"
"${WIREPROXY_BIN}" -c "${TEST_CONFIG}" > "${TEMP_DIR}/wireproxy.log" 2>&1 &
WIREPROXY_PID="$!"
sleep 3

if ! kill -0 "${WIREPROXY_PID}" 2>/dev/null; then
  sed -n '1,120p' "${TEMP_DIR}/wireproxy.log" >&2
  fail "wireproxy exited before the SOCKS5 check"
fi

PROXY_URL="socks5h://${SOCKS_HOST}:${SOCKS_PORT}"
printf 'Checking proxied IP...\n'
PROXY_IP="$(retry_curl 3 -4fsS --max-time 20 --proxy "${PROXY_URL}" "${IP_CHECK_URL}")"

printf 'Checking HTTPS connectivity...\n'
HTTPS_STATUS="$(retry_curl 3 -4fsS -o /dev/null -w '%{http_code}' --max-time 20 --proxy "${PROXY_URL}" "${HTTPS_CHECK_URL}")"
RSS_KB="$(ps -o rss= -p "${WIREPROXY_PID}" | awk '{print $1}')"

printf '\nwireproxy smoke test passed\n'
printf 'direct_ip=%s\n' "${DIRECT_IP}"
printf 'proxy_ip=%s\n' "${PROXY_IP}"
printf 'https_status=%s\n' "${HTTPS_STATUS}"
printf 'wireproxy_rss_kb=%s\n' "${RSS_KB:-unknown}"

if [[ "${DIRECT_IP}" == "${PROXY_IP}" ]]; then
  printf 'WARNING: direct and proxy IP are identical; verify the WireGuard endpoint and routing.\n' >&2
fi
