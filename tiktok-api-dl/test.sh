#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE_URL:-http://localhost:7788}"

LINK1="https://www.tiktok.com/@arctic.motion/video/7644267480856136991?is_from_webapp=1&sender_device=pc"
LINK2="https://www.tiktok.com/@japa1.zk/video/7644363342496173320?is_from_webapp=1&sender_device=pc"

OUT_DIR="$(mktemp -d)"
echo "Output dir: $OUT_DIR"
echo "Base URL:   $BASE"
echo

echo "== /health =="
curl -sS "$BASE/health" | tee "$OUT_DIR/health.json"
echo; echo

for i in 1 2; do
  LINK="$(eval echo \$LINK$i)"
  echo "== /fetch link$i =="
  curl -sS -G "$BASE/fetch" --data-urlencode "url=$LINK" -o "$OUT_DIR/fetch$i.json"
  python3 -m json.tool < "$OUT_DIR/fetch$i.json" | head -40
  echo "..."
  echo "  videos: $(python3 -c "import json;d=json.load(open('$OUT_DIR/fetch$i.json'));print(len(d.get('download',{}).get('video',[])))")"
  echo "  images: $(python3 -c "import json;d=json.load(open('$OUT_DIR/fetch$i.json'));print(len(d.get('download',{}).get('images',[])))")"
  echo "  music:  $(python3 -c "import json;d=json.load(open('$OUT_DIR/fetch$i.json'));print(len(d.get('download',{}).get('music',[])))")"
  echo

  echo "== /download video link$i =="
  curl -sS -G "$BASE/download" \
    --data-urlencode "url=$LINK" \
    --data-urlencode "type=video" \
    -o "$OUT_DIR/video$i.mp4" -w "  http=%{http_code} size=%{size_download} type=%{content_type}\n"

  echo "== /download music link$i =="
  curl -sS -G "$BASE/download" \
    --data-urlencode "url=$LINK" \
    --data-urlencode "type=music" \
    -o "$OUT_DIR/music$i.mp3" -w "  http=%{http_code} size=%{size_download} type=%{content_type}\n"
  echo
done

echo "== Files saved =="
ls -lh "$OUT_DIR"
