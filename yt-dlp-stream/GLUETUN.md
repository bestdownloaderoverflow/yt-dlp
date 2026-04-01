# yt-dlp-stream dengan Multi-VPN Rotation (Mullvad)

Arsitektur 3-instance dengan VPN rotation otomatis untuk menghindari rate limit YouTube/TikTok.

## Arsitektur

```
                    ┌─────────────────┐
                    │    Go Gateway   │  ← Entry point (port 9111)
                    │ (LB + failover) │
                    └────────┬────────┘
                             │
            ┌────────────────┼────────────────┐
            │                │                │
     ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
     │  gluetun-1  │  │  gluetun-2  │  │  gluetun-3  │  ← VPN containers
     │  (Mullvad)  │  │  (Mullvad)  │  │  (Mullvad)  │
     └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
            │                │                │
     ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
     │ ytdlp-1     │  │ ytdlp-2     │  │ ytdlp-3     │  ← Python extractor worker daemon
     │ (TCP 9487)  │  │ (TCP 9488)  │  │ (TCP 9489)  │
     └─────────────┘  └─────────────┘  └─────────────┘
```

## Fitur

1. **Load Balancing** - Request didistribusikan ke 3 worker
2. **Auto Retry** - Jika worker gagal, otomatis coba worker lain
3. **VPN Rotation** - Saat terdeteksi rate limit, VPN di-rotate (restart container)
4. **Health Check** - Monitoring kesehatan worker setiap 30 detik
5. **24h Auto Restart** - Worker di-restart setiap 24 jam untuk refresh IP

## Setup

### 1. Generate Mullvad WireGuard Config

1. Login ke https://mullvad.net/en/account/wireguard-config
2. Generate 3 WireGuard configurations (untuk load balancing)
3. Catat untuk masing-masing:
   - `PrivateKey` → `MULLVAD_KEY_X`
   - `Address` → `MULLVAD_ADDR_X`
   - `DNS` → biasanya `10.64.0.1`

### 2. Copy Environment File

```bash
cd yt-dlp-stream
cp .env.example .env
# Edit .env dengan credentials Mullvad Anda
nano .env
```

### 3. Edit Environment Variables

```bash
# Isi dengan credentials dari Mullvad
MULLVAD_KEY_1=aBCdEfGhIjKlMnOpQrStUvWxYz1234567890AbCdEfGg=
MULLVAD_ADDR_1=10.64.123.45/32

MULLVAD_KEY_2=bCD... (dst)
MULLVAD_KEY_3=cDE... (dst)
```

### 4. Jalankan

```bash
docker-compose -f docker-compose.gluetun.yml up -d
```

### 5. Verifikasi

```bash
# Cek status worker
curl http://localhost:9111/health

# Cek IP masing-masing VPN
curl http://localhost:8001/v1/publicip/ip
curl http://localhost:8002/v1/publicip/ip
curl http://localhost:8003/v1/publicip/ip
```

## Penggunaan

Gunakan gateway sebagai entry point:

```bash
# Fetch metadata
curl "http://localhost:9111/fetch?url=https://youtube.com/watch?v=xxxxx"

# Download
curl "http://localhost:9111/download?key=w1-encryptedkey"

# Stream video
curl "http://localhost:9111/stream/video?url=https://youtube.com/watch?v=xxxxx&quality=720"
```

## Troubleshooting

### Rate Limit Masih Terjadi

Jika masih terkena rate limit:

1. **Tambah delay antar request** di client
2. **Ganti region VPN** - Ubah `MULLVAD_COUNTRY_X` ke negara berbeda
3. **Tambah worker** - Edit docker-compose untuk menambah instance

### Cek Log

```bash
# Gateway log
docker logs -f ytdlp-gateway

# Worker log
docker logs -f ytdlp-stream-1

# VPN log
docker logs -f ytdlp-gluetun-1
```

### Manual VPN Rotation

```bash
# Restart specific worker
docker restart ytdlp-gluetun-1 ytdlp-stream-1
```

## Endpoint Gateway

| Endpoint | Method | Deskripsi |
|----------|--------|-----------|
| `/` | GET | Root info |
| `/health` | GET | Health check & worker status |
| `/fetch` | GET | Fetch video metadata |
| `/download` | GET | Download dengan key |
| `/stream/video` | GET | Stream video |
| `/stream/video-chunked` | GET | Stream video (chunked) |
| `/stream/mp3` | GET | Stream MP3 |
| `/stream/m4a` | GET | Stream M4A |
| `/tiktok` | POST | TikTok downloader |
| `/tunnel` | GET | Download tunnel |

## Port Mapping

| Service | Port | Deskripsi |
|---------|------|-----------|
| Gateway | 9111 | Entry point utama |
| Gluetun 1 | 8001 | Control server VPN 1 |
| Gluetun 2 | 8002 | Control server VPN 2 |
| Gluetun 3 | 8003 | Control server VPN 3 |
| ytdlp 1 | 9487 | API worker 1 |
| ytdlp 2 | 9488 | API worker 2 |
| ytdlp 3 | 9489 | API worker 3 |

## Legacy Single-Instance Mode

Untuk single instance tanpa gateway, gunakan docker-compose.yml biasa.
