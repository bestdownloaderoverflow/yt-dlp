# serverx-rs — Rust + PyO3 + yt-dlp

Rewrite of `serverx` (Python/FastAPI) in Rust using Axum + PyO3.

## Kenapa Rust?

| Aspek | Python (serverx) | Rust (serverx-rs) |
|-------|------------------|-------------------|
| Thread pool config | `ThreadPoolExecutor(max_workers=4)` | **Otomatis** (Tokio runtime) |
| Process manager | Gunicorn `-w 4` | **Tidak perlu** (single binary) |
| ASGI server | Uvicorn `--workers 4` | **Tidak perlu** (built-in) |
| Memory per request | ~15-30MB | ~2-5MB |
| Startup time | ~1-2 detik | ~50ms |
| yt-dlp extraction | 3-10 detik | 3-10 detik (sama, via PyO3) |

**Tidak ada angka worker yang perlu di-config.** Tokio `spawn_blocking()` auto-manage thread pool.

## Arsitektur

```
Request → Axum (async, Tokio) → spawn_blocking → PyO3 → import yt_dlp → extract_info()
                                      ↑
                          Tokio auto-manages threads
                          (no manual config needed)
```

## Requirements

- Rust 1.75+
- Python 3.10+ (untuk yt-dlp)
- yt-dlp (`pip install yt-dlp`)

## Development

```bash
# Install yt-dlp
pip install yt-dlp

# Build & run
cargo run

# Or with custom port
PORT=9000 cargo run
```

## Docker

```bash
# Build & run
docker compose up --build

# Production
docker compose -f docker-compose.yml up -d
```

## API Endpoints

Sama persis dengan `serverx`:

- `GET /` — Root info
- `GET /health` — Health check
- `POST /download` — Extract video/photo info

```bash
curl -X POST http://localhost:8025/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://x.com/username/status/123456789"}'
```

## Perbandingan Config

### Python (serverx) — banyak angka yang harus di-set:
```python
executor = ThreadPoolExecutor(max_workers=4)     # angka 1
uvicorn.run("main:app", workers=4)               # angka 2
# gunicorn -w 4 -k uvicorn.workers.UvicornWorker # angka 3
```

### Rust (serverx-rs) — tidak ada angka:
```rust
#[tokio::main]  // Tokio auto-manage everything
async fn main() {
    // spawn_blocking() auto-resize thread pool
    axum::serve(listener, app).await.unwrap();
}
```
