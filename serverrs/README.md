# serverrs — Rust + PyO3 + yt-dlp (TikTok Downloader)

Migrasi penuh `serverpy` (Python/FastAPI) ke Rust menggunakan Axum + PyO3.
100% feature parity dengan serverpy.

## Kenapa Rust?

| Aspek | Python (serverpy) | Rust (serverrs) |
|-------|-------------------|-----------------|
| Thread pool config | `ThreadPoolExecutor(max_workers=20)` | **Otomatis** (Tokio runtime) |
| Process manager | Gunicorn `-w 5` | **Tidak perlu** (single binary) |
| ASGI server | Uvicorn workers | **Tidak perlu** (built-in) |
| Memory per request | ~15-30MB | ~2-5MB |
| Startup time | ~1-2 detik | ~50ms |
| yt-dlp extraction | 3-10 detik | 3-10 detik (sama, via PyO3) |

## Arsitektur

```
Request → Axum (async, Tokio) → spawn_blocking → PyO3 → import yt_dlp → extract_info()
                                      ↑
                          Tokio auto-manages threads
```

## API Endpoints

| Method | Path | Deskripsi |
|--------|------|-----------|
| `POST` | `/tiktok` | Extract metadata + encrypted download links |
| `GET` | `/download` | Download file via encrypted token |
| `GET` | `/stream` | Stream video/audio dari CDN |
| `GET` | `/download-slideshow` | Generate slideshow video dari image post |
| `GET` | `/health` | Health check + Redis/VPN status |

## Fitur

- **Encryption/Decryption** — XOR cipher + base64url (compatible serverjs/serverpy)
- **Redis Caching** — Cache metadata yt-dlp dengan TTL 5 menit
- **Streaming Proxy** — reqwest streaming untuk download/stream
- **Slideshow** — FFmpeg concat images + audio ke MP4
- **VPN Reconnect** — Auto-reconnect Gluetun VPN saat IP diblokir
- **Auto Cleanup** — Temp folder cleanup setiap 15 menit

## Requirements

- Rust 1.75+
- Python 3.10+ (untuk yt-dlp via PyO3)
- FFmpeg (untuk slideshow)
- Redis (optional, untuk caching)

## Development

```bash
# Install yt-dlp (pastikan accessible dari Python)
pip install yt-dlp

# Build & run
cargo run

# Atau dengan custom port
PORT=9000 cargo run
```

## Docker

```bash
# Build & run dengan VPN
docker compose up --build

# Production
docker compose up -d
```

## Contoh Request

```bash
# Extract metadata
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@user/video/123456789"}'

# Health check
curl http://localhost:3021/health
```

## Struktur File

```
serverrs/
├── Cargo.toml
├── src/
│   ├── main.rs          # Axum server, routes, AppState
│   ├── config.rs        # Settings dari env vars
│   ├── encryption.rs    # XOR cipher + base64url
│   ├── ytdlp.rs         # PyO3 yt-dlp extraction
│   ├── response.rs      # JSON response builder
│   ├── stream.rs        # /download & /stream handlers
│   ├── slideshow.rs     # FFmpeg slideshow generation
│   ├── cleanup.rs       # Temp folder cleanup scheduler
│   ├── vpn.rs           # VPN reconnect manager
│   └── cache.rs         # Redis caching layer
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```
