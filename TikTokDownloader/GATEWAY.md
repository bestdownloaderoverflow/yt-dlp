# TikTokDownloader Gateway API

Headless REST gateway that mirrors [`tiktok-api-dl`](https://github.com/almafazi/tiktok-api-dl) / Douyin gateway contracts, powered by the **TikTokDownloader** engine.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Service info |
| `GET` | `/health` | — | Health + Redis/session backend |
| `GET` | `/metrics` | — | Request counters |
| `POST` | `/tiktok` | `X-API-Key` (if set) | Extract TikTok **or** Douyin URL (auto-detect) |
| `POST` | `/douyin` | `X-API-Key` (if set) | Extract Douyin URL |
| `GET` | `/tiktok/download` | — | Stream media by session `key` |

Docs (non-production): `http://127.0.0.1:7790/docs`

## Quick start (Docker)

```bash
cp .env.gateway.example .env.gateway
# edit DOUYIN_COOKIE / TIKTOK_COOKIE / TIKTOK_API_KEY

docker compose -f docker-compose.gateway.yml up -d --build
curl -s http://127.0.0.1:7790/health
```

## Quick start (local)

```bash
# Python >= 3.12
pip install -r requirements.txt -r requirements-gateway.txt
# optional: redis-server + REDIS_URL=redis://127.0.0.1:6379/0

export DOUYIN_COOKIE='...'   # recommended
export TIKTOK_COOKIE='...'   # optional
export SERVER_PORT=7790

python -m gateway.run_server
# or: uvicorn gateway.run_server:app --host 0.0.0.0 --port 7790
```

## Example

```bash
curl -s -X POST http://127.0.0.1:7790/tiktok \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_KEY' \
  -d '{"url":"https://www.tiktok.com/@user/video/1234567890"}'
```

**Video response (`status: tunnel`):**

```json
{
  "status": "tunnel",
  "extract_source": "tiktokdownloader",
  "title": "...",
  "download_link": {
    "no_watermark": "/tiktok/download?key=...",
    "no_watermark_hd": "/tiktok/download?key=...",
    "mp3": "/tiktok/download?key=..."
  },
  "author": { "nickname": "...", "uniqueId": "..." },
  "platform": "tiktok"
}
```

**Photo response (`status: picker`):**

```json
{
  "status": "picker",
  "photos": [{ "type": "photo", "url": "...", "download_link": "/tiktok/download?key=..." }],
  "download_slideshow": "/tiktok/download?key=...",
  "platform": "douyin"
}
```

Download:

```bash
curl -L -o video.mp4 'http://127.0.0.1:7790/tiktok/download?key=SESSION_KEY'
```

Session keys expire in **300s** (atomic claim). Extraction results are cached **1800s** (Redis if `REDIS_URL` is set).

## Environment

| Variable | Default | Notes |
|----------|---------|--------|
| `SERVER_HOST` | `0.0.0.0` | |
| `SERVER_PORT` | `7790` | |
| `REDIS_URL` | empty → memory | Required for multi-worker / durable sessions |
| `TIKTOK_API_KEY` | empty | Header `X-API-Key` |
| `DOUYIN_COOKIE` | | Injected into `Volume/settings.json` on boot |
| `TIKTOK_COOKIE` | | Same for TikTok |
| `PROXY` / `PROXY_TIKTOK` | | |
| `TIKTOK_EXTRACT_CACHE_TTL_SECONDS` | `1800` | |
| `SESSION_TTL_SECONDS` | `300` | |
| `ENVIRONMENT` | `development` | `production` disables `/docs` |
| `SLIDESHOW_MAX_CONCURRENT` | `1` | ffmpeg concurrency |

Cookies can also be written into `Volume/settings.json` via the normal CLI (`python main.py`).

## Notes / limitations

1. Upstream **encryption parameter algorithms may be expired** (upstream project warning). If extract fails, configure your own parameter generator (`encipher_example.py` / wiki) and valid cookies.
2. Gateway does **not** replace the legacy Web API on port `5555` (`python main.py` → Web API mode).
3. Slideshow rendering needs **ffmpeg** in `PATH` (included in `Dockerfile.gateway`).
4. For production multi-worker, use Redis and avoid in-memory sessions.

## Smoke test

```bash
chmod +x test_gateway.sh
./test_gateway.sh
TEST_URL='https://www.tiktok.com/@x/video/y' ./test_gateway.sh
```

## Layout

```
gateway/
  app.py              # FastAPI factory
  run_server.py       # uvicorn entry
  routes.py           # /tiktok /douyin /tiktok/download
  engine_adapter.py   # bootstrap Parameter + detail extract
  normalize.py        # extractor → tunnel/picker
  session.py          # Redis/memory download keys
  extraction_cache.py # extract cache
  slideshow.py        # ffmpeg photo → mp4
```
