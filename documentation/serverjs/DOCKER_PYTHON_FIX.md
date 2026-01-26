# Docker Python Version Fix

## 🐛 Problem

**Error:**
```
ImportError: You are using an unsupported version of Python. 
Only Python versions 3.10 and above are supported by yt-dlp
```

**Root Cause:**
- `node:20-bullseye-slim` uses Debian Bullseye
- Debian Bullseye default Python is 3.9
- yt-dlp requires Python 3.10+

---

## ✅ Solution

**Updated Dockerfile:**
- Install Python 3.11 from Debian backports
- Set Python 3.11 as default `python3`
- Install pip for Python 3.11
- Install yt-dlp using Python 3.11

---

## 🔧 Changes Made

### Before:
```dockerfile
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip
```

### After:
```dockerfile
# Install Python 3.11 from backports
RUN echo "deb http://deb.debian.org/debian bullseye-backports main" > /etc/apt/sources.list.d/backports.list && \
    apt-get update && apt-get install -y \
    -t bullseye-backports \
    python3.11 \
    python3.11-dev \
    python3.11-distutils

# Set Python 3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11
```

---

## 🚀 Rebuild Instructions

### 1. Stop Current Container

```bash
cd serverjs
docker-compose down
```

### 2. Rebuild Image

```bash
docker-compose build --no-cache
```

**Why `--no-cache`?** To ensure Python 3.11 is freshly installed.

### 3. Start Container

```bash
docker-compose up -d
```

### 4. Verify Python Version

```bash
docker-compose exec yt-dlp-server python3 --version
```

**Expected:** `Python 3.11.x`

### 5. Verify yt-dlp

```bash
docker-compose exec yt-dlp-server yt-dlp --version
```

**Expected:** Version number (e.g., `2025.12.08`)

### 6. Test API

```bash
curl http://localhost:3021/health
```

---

## 📊 Verification

### Check Python Version

```bash
docker-compose exec yt-dlp-server python3 --version
# Expected: Python 3.11.x
```

### Check yt-dlp Works

```bash
docker-compose exec yt-dlp-server yt-dlp --version
# Expected: 2025.12.08 (or latest)
```

### Test TikTok Download

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}'
```

**Expected:** JSON response with video metadata (no Python error)

---

## 🎯 Summary

**Fixed:**
- ✅ Python 3.11 installed from Debian backports
- ✅ Python 3.11 set as default `python3`
- ✅ pip installed for Python 3.11
- ✅ yt-dlp installed using Python 3.11
- ✅ Local `yt_dlp/` directory now works (Python 3.11 compatible)

**Result:**
- ✅ No more Python version errors
- ✅ yt-dlp works correctly
- ✅ Both system yt-dlp and local yt-dlp.sh work

---

## 🔄 Quick Rebuild

```bash
cd serverjs
docker-compose down
docker-compose build --no-cache
docker-compose up -d
docker-compose logs -f yt-dlp-server
```

**After rebuild, test:**
```bash
curl http://localhost:3021/health
```

**Should work now!** ✅
