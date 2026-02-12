# Project Overview: yt-dlp-tiktok

## Deskripsi
Proyek ini adalah **fork dari yt-dlp** (youtube-dl fork yang populer) dengan penambahan beberapa server API untuk mengekstrak video dari TikTok dan X (Twitter). 

yt-dlp adalah command-line audio/video downloader yang feature-rich, mendukung ribuan situs.

## Struktur Proyek

### 1. Core yt-dlp (Python)
- **Lokasi**: `/yt_dlp/`
- **Versi**: 2026.02.04
- **Python**: 3.10+ required
- **Fungsi**: Library utama untuk ekstraksi video dari berbagai platform

### 2. Server Implementations
Proyek ini memiliki 4 implementasi server API yang berbeda:

#### a. serverx (Python/FastAPI - Simple)
- **Lokasi**: `/serverx/`
- **Port**: 8000
- **Fitur**: Simple API, direct URLs (no encryption), no caching
- **Use case**: Development, simple deployments

#### b. serverpy (Python/FastAPI - Full-featured)
- **Lokasi**: `/serverpy/`
- **Fitur**: Encrypted URLs, Redis caching, VPN failover, streaming, slideshow support
- **Use case**: Production dengan fitur lengkap

#### c. serverrs (Rust + PyO3 - Full-featured)
- **Lokasi**: `/serverrs/`
- **Port**: 3021
- **Fitur**: 100% feature parity dengan serverpy, tapi lebih cepat dan efisien
- **Tech**: Axum, Tokio, PyO3, Redis, FFmpeg
- **Use case**: Production high-performance

#### d. serverx-rs (Rust + PyO3 - Simple)
- **Lokasi**: `/serverx-rs/`
- **Port**: 8025
- **Fitur**: Simple API seperti serverx, tapi dengan performa Rust
- **Tech**: Axum, Tokio, PyO3
- **Use case**: Development dengan performa tinggi

## Tech Stack

### Python (yt-dlp core)
- Python 3.10+
- hatchling (build system)
- ruff (linting)
- autopep8 (formatting)
- pytest (testing)

### Rust (server implementations)
- Axum (web framework)
- Tokio (async runtime)
- PyO3 (Python interop)
- serde (serialization)
- chrono (datetime)
- regex-lite (regex)

## Commands

### yt-dlp Development
```bash
# Install dependencies
pip install -e ".[default,dev]"

# Run tests
make test
python -m pytest -Werror

# Linting & formatting
make codetest
ruff check .
autopep8 --diff .

# Build
make all

# Clean
make clean
```

### Serverx (Python)
```bash
cd serverx
pip install -r requirements.txt
python main.py
# atau
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Serverx-rs (Rust)
```bash
cd serverx-rs
cargo run
# atau dengan custom port
PORT=9000 cargo run
```

### Serverrs (Rust)
```bash
cd serverrs
cargo run
# atau dengan Docker
docker compose up --build
```

## API Endpoints

### serverx / serverx-rs
- `GET /` - Root info
- `GET /health` - Health check
- `POST /download` - Extract video info (body: `{"url": "..."}`)

### serverpy / serverrs
- `POST /tiktok` - Extract metadata + encrypted links
- `GET /download` - Download via encrypted token
- `GET /stream` - Stream video/audio
- `GET /download-slideshow` - Generate slideshow
- `GET /health` - Health check

## Platform Support
- TikTok (tiktok.com, douyin.com)
- X/Twitter (twitter.com, x.com)

## Key Files
- `yt_dlp/YoutubeDL.py` - Main downloader class
- `yt_dlp/extractor/` - Site extractors
- `pyproject.toml` - Python project config
- `Makefile` - Build automation
