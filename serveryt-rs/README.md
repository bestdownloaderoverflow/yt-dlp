# serveryt-rs — Rust + PyO3 + yt-dlp (Stateless)

General-purpose video downloader API in Rust using Axum + PyO3.

Supports **all yt-dlp supported sites** (YouTube, TikTok, Instagram, Facebook, X/Twitter, etc.)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  POST /download                                                              │
│  Request → Axum (async) → spawn_blocking → PyO3 → yt_dlp.extract_info()     │
│                                                              ↓               │
│                                                    JSON → parse formats      │
│                                                              ↓               │
│                                          Generate signed URLs (HMAC-SHA256)  │
│                                                              ↓               │
│                                    Return formats with signed stream URLs    │
│                                              (expires in 2 minutes)          │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  GET /stream?url=xxx&format=yyy&expires=ts&sig=xxx — PyO3 Direct Streaming  │
│                                                                              │
│  Rate limit check → Signature verification → Expiry check                    │
│                                              ↓                               │
│                spawn_blocking → PyO3 → yt_dlp.download(output_stream)       │
│                                              ↓                               │
│                                    StreamBridge (Python file-like object)    │
│                                              ↓                               │
│                                    RustSenderWrapper.send_chunk(bytes)       │
│                                              ↓                               │
│                                    mpsc channel → tokio channel              │
│                                              ↓                               │
│                                    Axum Body::from_stream → HTTP Response    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Stateless**: No Redis or database required — uses signed URLs with HMAC-SHA256
- **URL Signature**: Stream links expire after 2 minutes (configurable)
- **Rate Limiting**: In-memory rate limiter (10 requests/minute per IP default)
- **PyO3 Direct Streaming**: Downloads piped directly from yt-dlp to client
- **Zero Disk I/O**: No temporary files, pure memory streaming
- **Session Integrity**: Cookies and headers preserved within same Python process
- **Full Protocol Support**: DASH, HLS, M3U8, HTTP progressive — all handled by yt-dlp

## Requirements

- Rust 1.75+
- Python 3.10+ (for yt-dlp)
- yt-dlp (local copy in `../yt_dlp`)

## Development

```bash
# Build & run
cargo run

# Or with custom port
PORT=9000 cargo run
```

## Docker

```bash
# Build & run (from serveryt-rs directory)
docker compose up --build

# Production
docker compose -f docker-compose.yml up -d
```

## API Endpoints

### `GET /` — Root info

### `GET /health` — Health check

### `POST /download` — Extract video/photo info

```bash
curl -X POST http://localhost:8026/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

Response includes signed stream URLs that expire in 2 minutes.

### `GET /stream?url=xxx&format=yyy&expires=ts&sig=xxx` — Stream media

Stream the media using the signed URL from `/download` response.

Parameters:
- `url` — Original video URL (URL-encoded)
- `format` — Format selector (e.g., `bestvideo+bestaudio/best`)
- `expires` — Unix timestamp when link expires
- `sig` — HMAC-SHA256 signature

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8026` | Server port |
| `BASE_URL` | `http://localhost:8026` | Base URL for stream links |
| `SIGNATURE_SECRET` | (random) | Secret key for HMAC signatures (MUST set in production) |
| `SIGNATURE_TTL` | `120` | Signed URL expiry in seconds (2 minutes) |
| `RATE_LIMIT_REQUESTS` | `10` | Max requests per window per IP |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds |
| `COOKIES_PATH` | (none) | Path to cookies.txt file |
| `YTDLP_TIMEOUT` | `45` | yt-dlp extraction timeout |
| `DOWNLOAD_TIMEOUT` | `300` | Stream download timeout |
