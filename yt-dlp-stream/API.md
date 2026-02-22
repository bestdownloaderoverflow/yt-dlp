# yt-dlp Stream API Documentation

## Overview

yt-dlp Stream API menyediakan endpoint untuk mengambil metadata dan mendownload video, audio, dan foto dari berbagai platform (YouTube, Twitter/X, TikTok, dll).

## Base URL

```
http://localhost:9487
```

## Endpoints

### 1. Fetch Metadata

**Endpoint:** `GET /fetch`

Mengambil metadata dari URL dan menghasilkan encrypted download links yang berlaku selama 5 menit.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| url | string | Yes | URL video/photo |
| proxy | string | No | Proxy URL (e.g., http://127.0.0.1:8080) |
| impersonate | string | No | Browser untuk TLS fingerprinting (chrome, safari, dll) |

**Response:**

```json
{
  "type": "video",
  "platform": "youtube",
  "id": "uelHwf8o7_U",
  "title": "Video Title",
  "uploader": "Channel Name",
  "duration": 267,
  "thumbnail": "https://i.ytimg.com/...",
  "expires_in": 300,
  "download_links": {
    "video": {
      "1080p": "/download?key=abc123...",
      "720p": "/download?key=def456...",
      "480p": "/download?key=ghi789...",
      "360p": "/download?key=jkl012..."
    },
    "mp3": "/download?key=mno345..."
  }
}
```

**Response untuk Twitter/X Photos:**

```json
{
  "type": "photos",
  "platform": "twitter",
  "id": "2025072438430797996",
  "title": "Tweet text...",
  "uploader": "username",
  "thumbnail": "https://pbs.twimg.com/...",
  "expires_in": 300,
  "photos": [
    {
      "index": 1,
      "width": 680,
      "height": 680,
      "url": "https://pbs.twimg.com/media/xxx.jpg?name=orig",
      "download_link": "/download?key=photo-1-abc..."
    },
    {
      "index": 2,
      "width": 400,
      "height": 400,
      "url": "https://pbs.twimg.com/media/yyy.jpg?name=orig",
      "download_link": "/download?key=photo-2-def..."
    }
  ]
}
```

**Example:**

```bash
# YouTube Video
curl "http://localhost:9487/fetch?url=https://youtube.com/watch?v=uelHwf8o7_U"

# Twitter Video
curl "http://localhost:9487/fetch?url=https://x.com/user/status/123456"

# Twitter Photos
curl "http://localhost:9487/fetch?url=https://x.com/user/status/789012"
```

---

### 2. Download

**Endpoint:** `GET /download`

Mendownload video, MP3, atau foto menggunakan encrypted key dari endpoint `/fetch`.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| key | string | Yes | Download key dari `/fetch` |
| download | boolean | No | Force download sebagai attachment (default: true) |

**Response:**
- Video: `video/mp4` streaming response (attachment)
- MP3: `audio/mpeg` streaming response (attachment)
- Photo: `image/*` streaming response (attachment)

**Error Response:**

```json
{
  "detail": "Download link expired or invalid"
}
```

**Example:**

```bash
# Download video (ganti dengan key dari response fetch)
curl "http://localhost:9487/download?key=abc123" -o video.mp4

# Download dengan inline (buka di browser)
curl "http://localhost:9487/download?key=abc123&download=false"

# Download MP3
curl "http://localhost:9487/download?key=mno345" -o audio.mp3

# Download photo
curl "http://localhost:9487/download?key=photo-1-abc" -o photo.jpg
```

---

### 3. Legacy Endpoints

#### Get Info
`GET /info?url=...`

Mengambil metadata tanpa generate download links.

#### Stream Video
`GET /stream/video?url=...&quality=1080`

Streaming video langsung (legacy, tanpa encrypted key).

#### Stream Video Chunked
`GET /stream/video-chunked?url=...&quality=1080`

Streaming video dengan metode chunked (Cobalt-style), optimal untuk video panjang.

#### Stream MP3 Chunked
`GET /stream/mp3-chunked?url=...`

Streaming MP3 dengan metode chunked, optimal untuk audio panjang.

#### Stream M4A
`GET /stream/m4a?url=...`

Streaming M4A tanpa ffmpeg (paling cepat).

#### Health Check
`GET /health`

Health check dan metrics.

#### Stats
`GET /stats`

Internal statistics.

---

## Download Link Expiration

Download links yang di-generate oleh `/fetch` memiliki karakteristik:

- **TTL:** 5 menit (300 detik)
- **Storage:** Redis (in-memory)
- **Format:** UUID v4 random
- **One-time use:** Bisa digunakan berkali-kali selama belum expired

Jika link sudah expired:

```bash
curl "http://localhost:9487/download?key=expired-key"
# Response: 404 {"detail": "Download link expired or invalid"}
```

---

## Supported Platforms

| Platform | Video | Audio | Photos |
|----------|-------|-------|--------|
| YouTube | ✅ | ✅ | ❌ |
| Twitter/X | ✅ | ✅ | ✅ |
| TikTok | ✅ | ✅ | ❌ |
| Instagram | ✅ | ✅ | ✅ |
| Facebook | ✅ | ✅ | ❌ |
| Dan 1000+ lainnya | ✅ | ✅ | ✅ |

---

## Error Handling

### 400 Bad Request

URL tidak valid atau tidak bisa di-extract.

```json
{
  "detail": "ERROR: [twitter] No video could be found in this tweet"
}
```

### 404 Not Found

Download key tidak ditemukan atau sudah expired.

```json
{
  "detail": "Download link expired or invalid"
}
```

### 500 Internal Server Error

Server error atau yt-dlp gagal memproses request.

```json
{
  "detail": "Internal server error"
}
```

---

## Docker Deployment

### Development

```bash
docker-compose --profile dev up -d
```

### Production

```bash
docker-compose --profile prod up -d
```

### Scaled (Multiple Instances)

```bash
docker-compose --profile scaled up -d
```

---

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│   /fetch    │────▶│    Redis    │
└─────────────┘     └─────────────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Metadata   │
                    │  + Links    │
                    └─────────────┘
                           │
                           ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│  /download  │────▶│   yt-dlp    │
└─────────────┘     └─────────────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │   Stream    │
                    │  Response   │
                    └─────────────┘
```

---

## Notes

1. **Chunked Streaming:** Video dan MP3 menggunakan metode chunked (10MB per chunk) untuk optimalisasi video/audio panjang.

2. **Photo Download:** Photo di-stream sebagai attachment (bukan redirect), sehingga selalu terdownload sebagai file.

3. **Redis:** Download sessions disimpan di Redis dengan max memory 256MB dan policy `allkeys-lru` (auto-evict keys jika penuh).

4. **Rate Limiting:** Endpoint `/fetch` memiliki rate limiting (gunakan `_enforce_rate_limit`).
