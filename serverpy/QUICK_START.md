# Quick Start Guide - ServerPY

Get up and running with the Python TikTok Downloader in 5 minutes.

## Prerequisites

- Python 3.8+ installed
- FFmpeg installed (for slideshow feature)
- yt_dlp library in parent directory

## Installation

### 1. Navigate to serverpy directory

```bash
cd /Users/almafazi/Documents/yt-dlp-tiktok/serverpy
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Or use the start script (recommended):

```bash
./start.sh
```

The start script will:
- Create virtual environment
- Install dependencies
- Create necessary directories
- Start the server

## Quick Test

### Test the server is running

```bash
curl http://localhost:3021/health
```

Expected response:
```json
{
  "status": "ok",
  "time": 1234567890.123,
  "ytdlp": "2024.01.26",
  "workers": {
    "max": 20,
    "active": 0
  }
}
```

### Test TikTok extraction

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url":"https://vt.tiktok.com/ZSaPXyDuw/"}'
```

## Common Issues

### 1. yt_dlp import error

**Error:** `ModuleNotFoundError: No module named 'yt_dlp'`

**Solution:** Make sure yt_dlp folder exists in parent directory:
```bash
ls ../yt_dlp/  # Should show __init__.py, extractor/, etc.
```

### 2. FFmpeg not found

**Error:** Slideshow generation fails

**Solution:** Install FFmpeg:
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Verify
ffmpeg -version
```

### 3. Port already in use

**Error:** `Address already in use`

**Solution:** Change port in `.env` or kill existing process:
```bash
# Find process using port 3021
lsof -ti:3021

# Kill it
lsof -ti:3021 | xargs kill -9
```

### 4. Permission denied

**Error:** `Permission denied: './start.sh'`

**Solution:** Make scripts executable:
```bash
chmod +x start.sh test.sh
```

## Running in Production

### Option 1: Uvicorn (Single Worker)

```bash
uvicorn main:app --host 0.0.0.0 --port 3021 --workers 1
```

### Option 2: Gunicorn + Uvicorn (Multiple Workers)

```bash
pip install gunicorn
gunicorn main:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:3021
```

### Option 3: Docker

```bash
# Build
docker build -t tiktok-downloader-py .

# Run
docker run -p 3021:3021 tiktok-downloader-py
```

### Option 4: Docker Compose

```bash
docker-compose up -d
```

## Configuration

Edit `.env` file:

```env
PORT=3021                           # Server port
BASE_URL=http://localhost:3021      # Base URL for download links
ENCRYPTION_KEY=overflow             # Encryption key (change this!)
MAX_WORKERS=20                      # Thread pool size
YTDLP_TIMEOUT=30                   # yt-dlp timeout (seconds)
DOWNLOAD_TIMEOUT=120                # Download timeout (seconds)
```

## Performance Tuning

### For Low Traffic (<100 req/hour)
```env
MAX_WORKERS=10
```

### For Medium Traffic (100-500 req/hour)
```env
MAX_WORKERS=20
```

### For High Traffic (>500 req/hour)
```env
MAX_WORKERS=50
```

**Note:** More workers = more memory usage. Monitor your server resources.

## Monitoring

### Check health

```bash
curl http://localhost:3021/health | python -m json.tool
```

### Monitor logs

```bash
# If running directly
python main.py

# If running with uvicorn
uvicorn main:app --log-level info

# If running with Docker
docker logs -f tiktok-downloader-py
```

### Check memory usage

```bash
ps aux | grep "python.*main.py"
```

## Testing

Run the test suite:

```bash
./test.sh
```

Expected output:
```
🧪 Testing TikTok Downloader API (Python)
==========================================

1️⃣  Testing /health endpoint... ✅ PASS
2️⃣  Testing /tiktok endpoint... ✅ PASS
3️⃣  Testing invalid URL handling... ✅ PASS
4️⃣  Testing missing URL handling... ✅ PASS
5️⃣  Testing CORS headers... ✅ PASS

🎉 All tests completed!
```

## Next Steps

1. **Security:** Change `ENCRYPTION_KEY` in `.env`
2. **Cookies:** Add TikTok cookies to `cookies/www.tiktok.com_cookies.txt`
3. **Monitoring:** Set up logging and alerting
4. **Scaling:** Deploy behind load balancer for high traffic
5. **Caching:** Add Redis for better performance

## Comparison with ServerJS

| Metric | ServerJS | ServerPY |
|--------|----------|----------|
| Startup time | Instant | Instant |
| Memory (idle) | 50MB | 150MB |
| Memory (100 concurrent) | 8GB 💥 | 300MB ✅ |
| Throughput | 3-4 req/s | 10-15 req/s |
| Max concurrent | Unlimited 💥 | 20 (safe) |

**Bottom line:** ServerPY uses more memory at idle, but scales much better under load.

## Support

- Check logs for errors
- Run `./test.sh` to diagnose issues
- Review `README.md` for detailed documentation
- See `COMPARISON.md` for performance analysis

## License

MIT
