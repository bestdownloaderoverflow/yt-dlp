#!/usr/bin/env bash
# Smoke test for TikTokDownloader gateway
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:7790}"
API_KEY="${TIKTOK_API_KEY:-}"
HDR=()
if [[ -n "$API_KEY" ]]; then
  HDR=(-H "X-API-Key: $API_KEY")
fi

echo "== GET / =="
curl -fsS "$BASE/" | head -c 400
echo -e "\n"

echo "== GET /health =="
curl -fsS "$BASE/health"
echo -e "\n"

echo "== GET /metrics =="
curl -fsS "$BASE/metrics"
echo -e "\n"

URL="${TEST_URL:-}"
if [[ -z "$URL" ]]; then
  echo "Set TEST_URL to run POST /tiktok extract test, e.g.:"
  echo "  TEST_URL='https://www.tiktok.com/@user/video/123' ./test_gateway.sh"
  exit 0
fi

echo "== POST /tiktok =="
RESP=$(curl -fsS -X POST "$BASE/tiktok" \
  "${HDR[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$URL\"}")
echo "$RESP" | head -c 1200
echo -e "\n"

KEY=$(python3 - <<'PY' "$RESP"
import json,sys
d=json.loads(sys.argv[1])
links=d.get("download_link") or {}
if isinstance(links.get("no_watermark"), str):
    print(links["no_watermark"].split("key=")[-1])
elif isinstance(links.get("no_watermark"), list) and links["no_watermark"]:
    print(links["no_watermark"][0].split("key=")[-1])
elif links.get("no_watermark_hd"):
    print(str(links["no_watermark_hd"]).split("key=")[-1])
else:
    print("")
PY
)

if [[ -n "$KEY" ]]; then
  echo "== GET /tiktok/download (headers only) =="
  curl -sS -D - -o /dev/null "$BASE/tiktok/download?key=$KEY&download=false" | head -n 20
else
  echo "No download key in response (extract may have failed — check cookie/encrypt)"
fi
