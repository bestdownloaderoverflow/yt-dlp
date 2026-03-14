# Panduan Penggunaan yt-dlp-stream

## 1. Menjalankan Aplikasi

### Opsi A - Docker (disarankan)

```bash
cd /Users/almafazi/Documents/yt-dlp-tiktok/yt-dlp-stream
docker compose --profile dev up -d
```

API default: `http://localhost:9487`

### Opsi B - Lokal (tanpa Docker)

```bash
cd /Users/almafazi/Documents/yt-dlp-tiktok/yt-dlp-stream
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

API default: `http://localhost:8000`

## 2. Alur Utama (YouTube, X/Twitter, dan platform umum)

Flow utama aplikasi:
1. `GET /fetch?url=...` untuk ambil metadata + link download terenkripsi.
2. `GET /download?key=...` untuk download file (video/mp3/photo).

`key` berlaku 5 menit.

### Contoh: YouTube

```bash
curl "http://localhost:9487/fetch?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

Ambil salah satu `download_links.video["720p"]` atau `download_links.mp3`, lalu panggil:

```bash
curl -L "http://localhost:9487/download?key=ISI_KEY_VIDEO" -o video.mp4
curl -L "http://localhost:9487/download?key=ISI_KEY_MP3" -o audio.mp3
```

### Contoh: X/Twitter

```bash
curl "http://localhost:9487/fetch?url=https://x.com/<user>/status/<id>"
curl -L "http://localhost:9487/download?key=ISI_KEY_VIDEO" -o x-video.mp4
```

## 3. Alur Khusus TikTok

TikTok memakai endpoint terpisah:

1. `POST /tiktok` untuk ambil metadata + link download terenkripsi.
2. `GET /tiktok/download?key=...` untuk download video/mp3/photo/slideshow.

### Contoh request

```bash
curl -X POST "http://localhost:9487/tiktok" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.tiktok.com/@scout2015/video/6718335390845095173",
    "proxy": null,
    "impersonate": "chrome"
  }'
```

Lalu download:

```bash
curl -L "http://localhost:9487/tiktok/download?key=ISI_KEY" -o tiktok-output.mp4
```

## 4. Endpoint Penting Lain

- `GET /docs` Swagger UI.
- `GET /health` health check.
- `GET /stats` statistik internal.
- `GET /stream/video`, `GET /stream/mp3`, `GET /stream/video-chunked`, dll untuk mode streaming langsung.

## 5. Catatan Operasional

- Redis wajib aktif untuk cache session download.
- Jika dapat error `Download link expired or invalid`, ulangi request `/fetch` (atau `/tiktok`) untuk key baru.
- Untuk situs sensitif (misalnya TikTok), opsi `impersonate` bisa membantu stabilitas.
