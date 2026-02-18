# YouTube Downloader FastAPI Server

Server FastAPI untuk download video YouTube menggunakan yt-dlp embed dengan asyncio dan uvicorn.

## Fitur

- `/fetch` - Mengambil informasi video tanpa download
- `/process` - Memulai proses download video
- `/check-progress/{download_id}` - Cek progress download
- `/download/{download_id}` - Download file yang sudah selesai (auto hapus setelah download)
- Support download MP3
- Support pilih format video
- Auto cleanup file temporary

## Instalasi

```bash
# Buat virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate  # Linux/Mac
# atau
venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

## Cara Menjalankan

```bash
# Activate virtual environment
source venv/bin/activate

# Jalankan server
python main.py
# atau
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Server akan berjalan di `http://localhost:8000`

## API Documentation

Buka `http://localhost:8000/docs` untuk melihat dokumentasi API interaktif (Swagger UI).

## Endpoint API

### 1. Fetch Video Info
```bash
POST /fetch
Content-Type: application/json

{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

### 2. Process/Download Video
```bash
POST /process
Content-Type: application/json

{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "format_id": "22",  // optional - pilih format spesifik
  "download_mp3": false  // true untuk download MP3
}
```

Response:
```json
{
  "download_id": "uuid-string",
  "title": "Video Title",
  "duration": 120,
  "thumbnail": "https://...",
  "formats": [...]
}
```

### 3. Check Progress
```bash
GET /check-progress/{download_id}
```

Response:
```json
{
  "download_id": "uuid-string",
  "status": "downloading",
  "progress": 45.5,
  "speed": "1.5MiB/s",
  "eta": "00:30",
  "filename": "/path/to/file",
  "error": null
}
```

### 4. Download File
```bash
GET /download/{download_id}
```

File akan didownload dan otomatis dihapus dari server setelah selesai.

### 5. List Active Downloads
```bash
GET /active-downloads
```

### 6. Manual Cleanup
```bash
DELETE /cleanup/{download_id}
```

## Contoh Penggunaan dengan cURL

### Fetch Info
```bash
curl -X POST "http://localhost:8000/fetch" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

### Download Video
```bash
curl -X POST "http://localhost:8000/process" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

### Download MP3
```bash
curl -X POST "http://localhost:8000/process" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "download_mp3": true
  }'
```

### Check Progress
```bash
curl "http://localhost:8000/check-progress/YOUR_DOWNLOAD_ID"
```

### Download File
```bash
curl "http://localhost:8000/download/YOUR_DOWNLOAD_ID" \
  -o "video.mp4"
```

## Catatan

- File temporary disimpan di folder `temp_downloads/`
- File otomatis dihapus setelah user download
- Gunakan endpoint `/cleanup` untuk manual cleanup jika diperlukan
