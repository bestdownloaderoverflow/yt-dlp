#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE_URL:-http://localhost:7788}"

VIDEO_LINK1="https://www.tiktok.com/@arctic.motion/video/7644267480856136991?is_from_webapp=1&sender_device=pc"
VIDEO_LINK2="https://www.tiktok.com/@japa1.zk/video/7644363342496173320?is_from_webapp=1&sender_device=pc"
SLIDESHOW_LINK="https://www.tiktok.com/@yusuf_sufiandi24/photo/7457053391559216392?is_from_webapp=1&sender_device=pc"

OUT_DIR="$(mktemp -d)"
echo "Output dir: $OUT_DIR"
echo "Base URL:   $BASE"
echo

echo "== GET / =="
curl -sS "$BASE/" | python3 -m json.tool
echo; echo

echo "== GET /health =="
curl -sS "$BASE/health" | python3 -m json.tool
echo; echo

test_video() {
  local label="$1"
  local link="$2"

  echo "========== $label =="
  echo "== POST /tiktok =="
  curl -sS -X POST "$BASE/tiktok" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"$link\"}" \
    -o "$OUT_DIR/$label.json" -w "http=%{http_code} size=%{size_download}\n"

  python3 -c "
import json
d = json.load(open('$OUT_DIR/$label.json'))
print('status:', d.get('status'))
print('extract_source:', d.get('extract_source'))
print('author.uniqueId:', d.get('author',{}).get('uniqueId'))
print('artist:', d.get('artist'))
print('duration (ms):', d.get('duration'))
print('statistics:', d.get('statistics'))
dl = d.get('download_link', {})
if isinstance(dl.get('no_watermark'), list):
    print('photos count:', len(d.get('photos', [])))
    print('download_slideshow:', d.get('download_slideshow'))
    print('download_link.no_watermark (first):', dl['no_watermark'][0] if dl.get('no_watermark') else None)
else:
    print('download_link keys:', list(dl.keys()))
    for k, v in dl.items():
        print(' ', k, '->', v[:60] if isinstance(v,str) else v)
if d.get('error'): print('ERROR:', d['error'])
"
  echo

  # Pick the first available key and download
  KEY=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/$label.json'))
dl = d.get('download_link', {})
if isinstance(dl.get('no_watermark'), list):
    print(dl['no_watermark'][0].split('key=')[1] if dl.get('no_watermark') else '')
else:
    for q in ['no_watermark_hd','no_watermark','watermark','mp3']:
        v = dl.get(q)
        if v:
            print(v.split('key=')[1]); break
    else:
        print('')
")

  if [ -n "$KEY" ]; then
    echo "== GET /tiktok/download (first key) =="
    EXT="mp4"
    [ "$(python3 -c "import json;d=json.load(open('$OUT_DIR/$label.json'));print(d.get('status'))")" = "picker" ] && EXT="jpg"
    curl -sS "$BASE/tiktok/download?key=$KEY" \
      -o "$OUT_DIR/${label}_dl.${EXT}" -w "http=%{http_code} size=%{size_download} type=%{content_type}\n"
    printf "signature: "; head -c 12 "$OUT_DIR/${label}_dl.${EXT}" | xxd | head -1
    echo
  fi
}

test_video "video1" "$VIDEO_LINK1"
echo
test_video "video2" "$VIDEO_LINK2"
echo

echo "========== slideshow =="
echo "== POST /tiktok (photo post) =="
curl -sS -X POST "$BASE/tiktok" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$SLIDESHOW_LINK\"}" \
  -o "$OUT_DIR/slideshow.json" -w "http=%{http_code} size=%{size_download}\n"

python3 -c "
import json
d = json.load(open('$OUT_DIR/slideshow.json'))
print('status:', d.get('status'))
print('photos count:', len(d.get('photos', [])))
print('download_slideshow:', d.get('download_slideshow'))
print('download_link.mp3:', d.get('download_link',{}).get('mp3'))
if d.get('error'): print('ERROR:', d['error'])
"
echo

SLIDESHOW_KEY=$(python3 -c "import json;d=json.load(open('$OUT_DIR/slideshow.json'));print((d.get('download_slideshow') or '').split('key=')[-1])")

if [ -n "$SLIDESHOW_KEY" ]; then
  echo "== GET /tiktok/download (slideshow key -> ffmpeg MP4) =="
  echo "Rendering slideshow via ffmpeg... (may take a few seconds)"
  curl -sS "$BASE/tiktok/download?key=$SLIDESHOW_KEY" \
    -o "$OUT_DIR/slideshow.mp4" -w "http=%{http_code} size=%{size_download} type=%{content_type}\n"
  printf "signature: "; head -c 12 "$OUT_DIR/slideshow.mp4" | xxd | head -1
  echo
fi

echo
echo "== Files saved =="
ls -lh "$OUT_DIR"
