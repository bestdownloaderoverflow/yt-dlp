# TikTok/X Video Downloader API

Simple API untuk mengekstrak informasi video dan link download dari TikTok dan X (Twitter) menggunakan yt-dlp Python wrapper.

## Fitur

- ✅ Dukungan TikTok dan X (Twitter)
- ✅ Response langsung dengan link download (tidak perlu enkripsi/decrypt)
- ✅ Multiple video qualities (HD, SD, dll)
- ✅ Audio-only download support
- ✅ Metadata lengkap (author, stats, thumbnail, dll)
- ✅ FastAPI dengan auto-generated docs

## Instalasi

```bash
# Install dependencies (Python 3.10+ required)
pip install -r requirements.txt

# Or with specific Python version
python3.11 -m pip install -r requirements.txt
```

## Menjalankan Server

```bash
# Development (with auto-reload)
python main.py

# Or with uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

## API Endpoints

### 1. Root Info
```bash
GET /
```

### 2. Health Check
```bash
GET /health
```

### 3. Extract Video (Main Endpoint)
```bash
POST /download
Content-Type: application/json

{
  "url": "https://x.com/username/status/123456789"
}
```

## Response Structure

### Success Response
```json
{
  "success": true,
  "message": "Video info extracted successfully",
  "data": {
    "platform": "x",
    "video_id": "2017334134776205312",
    "title": "Video title...",
    "description": "Video description...",
    "author_name": "Username",
    "author_username": "handle",
    "author_avatar": "https://...",
    "thumbnail": "https://...",
    "duration_seconds": 33.33,
    "duration_formatted": "0:33",
    "stats": {
      "views": 1000,
      "likes": 181,
      "comments": 17,
      "shares": 13
    },
    "created_at": "2026-01-31",
    "original_url": "https://x.com/..."
  },
  "video_formats": [
    {
      "quality": "1080p (progressive)",
      "resolution": "1920x1080",
      "url": "https://video.twimg.com/...",
      "size_bytes": 43253760,
      "format_id": "http-10368"
    }
  ],
  "audio_formats": [
    {
      "quality": "128kbps",
      "resolution": "audio only",
      "url": "https://video.twimg.com/...",
      "size_bytes": null,
      "format_id": "hls-audio-128000-Audio"
    }
  ],
  "best_video_url": "https://video.twimg.com/...",
  "best_audio_url": "https://video.twimg.com/...",
  "extracted_at": "2026-02-01T10:14:30.123456Z"
}
```

### Error Response
```json
{
  "success": false,
  "message": "Video not found or may be private/deleted",
  "error_code": "HTTP_404"
}
```

## Testing

### Test dengan cURL

```bash
# Test X (Twitter) URL
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://x.com/windsurf/status/2017679700630659449"}'

# Test TikTok URL
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@username/video/123456789"}'

# Pretty print JSON
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://x.com/JulianGoldieSEO/status/2017655446618915041"}' | jq

# Save response to file
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://x.com/windsurf/status/2017679700630659449"}' \
  -o response.json
```

### Test dengan Python

```bash
# Install requests
pip install requests

# Run tests
python example_usage.py

# Download video directly
python example_usage.py download "https://x.com/windsurf/status/2017679700630659449" video.mp4
```

### Test URLs

**X (Twitter):**
- `https://x.com/windsurf/status/2017679700630659449`
- `https://x.com/JulianGoldieSEO/status/2017655446618915041`

**TikTok:**
- `https://www.tiktok.com/@username/video/1234567890`

## API Documentation

Setelah server berjalan, akses:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json

## Perbedaan dengan serverpy

| Fitur | serverx (ini) | serverpy |
|-------|--------------|----------|
| Response style | Langsung (direct URLs) | Terenkripsi |
| Download flow | Client langsung download | Via endpoint `/download` |
| Slideshow | Tidak support | Support dengan FFmpeg |
| Caching | Tidak ada | Redis caching |
| VPN Failover | Tidak ada | Ada |
| Streaming | Tidak ada | Ada endpoint `/stream` |
| Complexity | Simple | Complex |

## Environment Variables

```bash
PORT=8000  # Server port (default: 8000)
```

## Catatan

- Python 3.10+ diperlukan untuk yt-dlp
- Link download yang diberikan bersifat direct URL dari platform aslinya
- Untuk download, client langsung mengakses URL yang diberikan
- Tidak ada enkripsi/token seperti di serverpy
