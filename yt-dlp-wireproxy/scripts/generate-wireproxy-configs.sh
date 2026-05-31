#!/usr/bin/env bash
set -euo pipefail

# generate-wireproxy-configs.sh
# Recursively reads all .conf files from ../surfshark-pay, then appends
# ../wgcf/wgcf-panji/wgcf-profile.conf and ../mullvad-new/*.conf. The output
# is copied to runtime-configs/ with the [Socks5] section required by wireproxy.
#
# Output directory: <project>/runtime-configs/ (git-ignored)
# Container naming: wireproxy-01 through wireproxy-18

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SURFSHARK_DIR="${PROJECT_DIR}/../surfshark-pay"
WGCF_CONFIG="${PROJECT_DIR}/../wgcf/wgcf-panji/wgcf-profile.conf"
MULLVAD_DIR="${PROJECT_DIR}/../mullvad-new"
DEST_DIR="${PROJECT_DIR}/runtime-configs"

usage() {
  cat <<'EOF'
Usage: ./scripts/generate-wireproxy-configs.sh

Reads all .conf files from ../surfshark-pay recursively, then appends
../wgcf/wgcf-panji/wgcf-profile.conf and ../mullvad-new/*.conf. Copies them to
./runtime-configs/ as wireproxy-01.conf through wireproxy-18.conf and appends a
[Socks5] listener section.

The dest dir (runtime-configs/) is git-ignored.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ ! -d "${SURFSHARK_DIR}" ]]; then
  echo "ERROR: source directory not found: ${SURFSHARK_DIR}" >&2
  exit 1
fi

if [[ ! -f "${WGCF_CONFIG}" ]]; then
  echo "ERROR: wgcf config not found: ${WGCF_CONFIG}" >&2
  exit 1
fi

if [[ ! -d "${MULLVAD_DIR}" ]]; then
  echo "ERROR: mullvad source directory not found: ${MULLVAD_DIR}" >&2
  exit 1
fi

# Collect all .conf files deterministically.
CONF_FILES=()
while IFS= read -r -d '' f; do
  CONF_FILES+=("$f")
done < <(find "${SURFSHARK_DIR}" -type f -name '*.conf' -print0 | sort -z)

CONF_FILES+=("${WGCF_CONFIG}")

while IFS= read -r -d '' f; do
  CONF_FILES+=("$f")
done < <(find "${MULLVAD_DIR}" -maxdepth 1 -type f -name '*.conf' -print0 | sort -z)

COUNT="${#CONF_FILES[@]}"

echo "Found ${COUNT} WireGuard configs"

if [[ "${COUNT}" -ne 18 ]]; then
  echo "WARNING: expected 18 configs, found ${COUNT}" >&2
fi

rm -rf "${DEST_DIR}"
mkdir -p "${DEST_DIR}"
chmod 700 "${DEST_DIR}"

echo ""
echo "=== Generated configs ==="

for i in "${!CONF_FILES[@]}"; do
  idx=$((i + 1))
  src="${CONF_FILES[$i]}"
  name="wireproxy-$(printf '%02d' "${idx}")"
  dest="${DEST_DIR}/${name}.conf"

  # Copy source config
  cp "${src}" "${dest}"

  # Append [Socks5] section if not already present
  if ! grep -qE '^\[Socks5\]' "${dest}"; then
    cat >> "${dest}" <<'WIRESECTION'

[Socks5]
BindAddress = 0.0.0.0:1080
WIRESECTION
  fi

  chmod 600 "${dest}"

  # Detect country from filename
  country=""
  base="$(basename "${src}" .conf)"
  base="${base%% (*}"  # strip parenthetical numbers like " (3)"
  case "${base}" in
    id-jak)  country="Indonesia" ;;
    id-jpu*) country="Indonesia" ;;
    sg-sng)  country="Singapore"  ;;
    sg-sin*) country="Singapore"  ;;
    vn-hcm)  country="Vietnam"   ;;
    wgcf-profile) country="WARP" ;;
    *)       country="unknown"    ;;
  esac

  echo "  ${name}.conf <- $(realpath --relative-to="${PROJECT_DIR}" "${src}" 2>/dev/null || echo "${src}") [${country}]"
done

echo ""
echo "Done. ${COUNT} configs written to ${DEST_DIR}/"
echo "Run: docker compose -f docker-compose.wireproxy.yml up -d"
