# gateway-go

Rewrite Go untuk `yt-dlp-stream/gateway/gateway.py`.

## Fitur

- HTTP gateway untuk endpoint:
  - `/`, `/fetch`, `/download`, `/info`
  - `/stream/video`, `/stream/video-chunked`, `/stream/mp3`, `/stream/mp3-chunked`, `/stream/m4a`
  - `/tiktok`, `/tiktok/download`, `/tunnel`, `/health`
- Retry + failover antar worker (`w1..wN`)
- Sliding-window rate limit per IP pada `/fetch` dan `/download`
- Deteksi rate-limit response (403/429 + pattern body)
- Scheduler restart worker dengan backoff, budget, dan quarantine
- Restart container worker via Docker CLI (`docker restart`)
- Circuit breaker + drain before restart (untuk mengurangi request putus saat restart)
- IPC extractor mode penuh (Go HTTP + Python extractor daemon), tanpa FastAPI/Granian worker HTTP

## Menjalankan lokal

```bash
cd yt-dlp-stream/gateway-go
go run .
```

Gateway listen di port `9111` (default).

## Environment Variables

- `GATEWAY_PORT` (default `9111`)
- `WORKER_COUNT` (default `3`)
- `EXTRACTOR_IPC_ENABLED` (default `false`)
- `EXTRACTOR_PYTHON_BIN` (default `python3`)
- `EXTRACTOR_WORKER_PATH` (default `../extractor/worker_daemon.py`)
- `EXTRACTOR_TIMEOUT_MS` (default `45000`)
- `GLUETUN_PASSWORD` (default `secretpassword`)
- `MAX_RETRIES` (default `3`)
- `HEALTH_CHECK_TIMEOUT_MS` (default `8000`)
- `HEALTH_MONITOR_INTERVAL_MS` (default `5000`)
- `HEALTH_FAILURE_THRESHOLD` (default `3`)
- `RATE_LIMIT_COOLDOWN` (default `300`)
- `RESTART_BACKOFF_BASE` (default `30`)
- `RESTART_BACKOFF_MAX` (default `300`)
- `RESTART_BUDGET_LIMIT` (default `3`)
- `RESTART_BUDGET_WINDOW` (default `600`)
- `RESTART_QUARANTINE_SECONDS` (default `600`)
- `RESTART_BACKOFF_JITTER` (default `5`)
- `DEGRADED_RETRY_AFTER` (default `5`)
- `GATEWAY_RL_WINDOW_SECONDS` (default `60`)
- `GATEWAY_RL_FETCH_LIMIT` (default `45`)
- `GATEWAY_RL_DOWNLOAD_LIMIT` (default `45`)
- `DRAIN_TIMEOUT_SECONDS` (default `90`)
- `DRAIN_POLL_INTERVAL_MS` (default `500`)

## Error Policy Matrix

Ringkasan kebijakan gateway saat menerima response dari worker:

| Worker status | Gateway action |
|---|---|
| `403` / `429` | Failover + schedule restart container (dengan circuit breaker + drain) |
| `400` | Failover tanpa restart |
| `4xx` lain | Return as-is ke client |
| `5xx` / network error | Retry failover (tanpa auto-restart by default) |

Dokumen detail mapping exception worker -> status HTTP -> aksi gateway ada di:

- [`ERROR_MAPPING.md`](/Users/almafazi/Documents/yt-dlp-tiktok/yt-dlp-stream/ERROR_MAPPING.md)

## Docker

Folder ini sudah menyediakan `Dockerfile`.

Contoh service compose:

```yaml
gateway-go:
  build:
    context: .
    dockerfile: gateway-go/Dockerfile
  container_name: ytdlp-gateway-go
  ports:
    - "9111:9111"
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
  environment:
    - GATEWAY_PORT=9111
    - WORKER_COUNT=3
    - EXTRACTOR_IPC_ENABLED=false
    - GLUETUN_PASSWORD=${GLUETUN_PASSWORD:-secretpassword}
    - MAX_RETRIES=3
    - RATE_LIMIT_COOLDOWN=300
  restart: unless-stopped
  networks:
    - ytdlp-network
```

## IPC Extractor Mode (Current)

Mode ini menyalakan Python extractor daemon per worker (TCP JSON-RPC) dan dipakai penuh oleh endpoint gateway:
- `GET /info`
- `GET /fetch`
- `GET /download`
- `GET /stream/video`
- `GET /stream/video-chunked`
- `GET /stream/mp3`
- `GET /stream/mp3-chunked`
- `GET /stream/m4a`
- `POST /tiktok`
- `GET /tiktok/download` (video/photo/mp3/slideshow)

```bash
cd yt-dlp-stream/gateway-go
EXTRACTOR_IPC_ENABLED=true \
EXTRACTOR_PYTHON_BIN=python3 \
EXTRACTOR_WORKER_PATH=../extractor/worker_daemon.py \
WORKER_COUNT=3 \
go run .
```

Catatan:
- Worker Python tidak lagi expose HTTP API FastAPI/Granian untuk jalur publik.
- Go gateway meng-handle HTTP, failover, retry, ffmpeg merge/transcode, dan streaming response.
- Python worker fokus pada extraction/resolution + session/cookie context per worker.

## Integration Test (Live Stack)

Test live gateway tersedia di:
- `yt-dlp-stream/tests/test_gateway_integration.py`

Jalankan saat compose stack sudah aktif:

```bash
cd yt-dlp-stream
RUN_GATEWAY_INTEGRATION=1 python3 -m unittest tests.test_gateway_integration
```
