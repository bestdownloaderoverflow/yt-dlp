# Docker Setup Guide

## 🐳 Docker & Docker Compose Setup

Complete guide untuk menjalankan yt-dlp TikTok server menggunakan Docker.

---

## 📋 Prerequisites

- Docker Engine 20.10+
- Docker Compose 2.0+
- At least 2GB RAM
- At least 5GB disk space

---

## 🚀 Quick Start

### 1. Build and Start

**Important:** Run commands from `serverjs/` directory:

```bash
cd serverjs
docker-compose up -d
```

**Why?** Docker Compose uses parent directory as build context to access `yt-dlp.sh` and `yt_dlp/` directory.

### 2. Check Status

```bash
docker-compose ps
```

### 3. View Logs

```bash
docker-compose logs -f yt-dlp-server
```

### 4. Stop

```bash
docker-compose down
```

---

## 📁 File Structure

```
yt-dlp-tiktok/              # Build context (parent directory)
├── yt-dlp.sh              # yt-dlp wrapper script (copied to Docker)
├── yt_dlp/                 # yt-dlp Python package (copied to Docker)
├── .dockerignore           # Files to exclude from Docker build
└── serverjs/
    ├── Dockerfile          # Docker image definition
    ├── docker-compose.yml  # Docker Compose configuration
    ├── package.json        # Node.js dependencies
    ├── index.js            # Main application
    └── temp/               # Temporary files (mounted volume)
```

**Note:** Build context is parent directory (`yt-dlp-tiktok/`), not `serverjs/`. This allows Docker to access `yt-dlp.sh` and `yt_dlp/` directory.

---

## 🔧 Configuration

### Environment Variables

Edit `docker-compose.yml` untuk mengubah konfigurasi:

```yaml
environment:
  - PORT=3021                    # Server port
  - BASE_URL=http://localhost:3021  # Public URL (change in production!)
  - ENCRYPTION_KEY=overflow       # Encryption key (change in production!)
  - YT_DLP_PATH=/usr/local/bin/yt-dlp.sh
  - FFMPEG_PATH=ffmpeg
  - TEMP_DIR=./temp
```

### Production Configuration

**IMPORTANT:** Sebelum deploy ke production, ubah:

1. **BASE_URL** - Set ke public URL Anda
   ```yaml
   - BASE_URL=https://api.yourdomain.com
   ```

2. **ENCRYPTION_KEY** - Gunakan key yang secure
   ```yaml
   - ENCRYPTION_KEY=your-secure-random-key-here
   ```

3. **Port Mapping** - Sesuaikan dengan kebutuhan
   ```yaml
   ports:
     - "3021:3021"  # host:container
   ```

---

## 🏗️ Dockerfile Details

### Base Image
- `node:20-bullseye-slim` - Lightweight Node.js 20 image

### Installed Packages
- **ffmpeg** - Video/audio processing
- **python3** - Required for yt-dlp
- **python3-pip** - Python package manager
- **yt-dlp** - TikTok video downloader (installed via pip)
- **curl** - Health check utility

### Build Context
- **Build context:** Parent directory (`/Users/almafazi/Documents/yt-dlp-tiktok`)
- **Dockerfile location:** `serverjs/Dockerfile`
- This allows copying `yt-dlp.sh` and `yt_dlp/` directory from parent

### Build Process
1. Install system dependencies (ffmpeg, python3, pip, curl)
2. Install yt-dlp via pip (system-wide)
3. Copy `yt-dlp.sh` and `yt_dlp/` directory from parent (for local yt-dlp)
4. Copy `serverjs/package.json` and install Node.js dependencies
5. Copy `serverjs/` application code
6. Create temp directory with proper permissions
7. Create yt-dlp wrapper script (uses local yt-dlp.sh if available, falls back to system yt-dlp)
8. Set up health check

---

## 📊 Docker Compose Services

### yt-dlp-server

**Image:** Built from Dockerfile  
**Port:** 3021  
**Restart:** unless-stopped  
**Health Check:** Every 30 seconds

**Volumes:**
- `./temp:/app/temp` - Temporary files directory

**Networks:**
- `tiktok_network` - Bridge network

---

## 🧪 Testing

### 1. Build Image

**Important:** Run from `serverjs/` directory:

```bash
cd serverjs
docker-compose build
```

**Build context:** Parent directory (`yt-dlp-tiktok/`)  
**Dockerfile:** `serverjs/Dockerfile`

This allows Docker to copy:
- `yt-dlp.sh` from parent directory
- `yt_dlp/` directory from parent directory
- `serverjs/` application code

### 2. Start Container

```bash
docker-compose up -d
```

### 3. Check Health

```bash
curl http://localhost:3021/health
```

**Expected Response:**
```json
{
  "status": "ok",
  "time": "2026-01-26T...",
  "ytdlp": "2025.12.08"
}
```

### 4. Test API

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}'
```

### 5. View Logs

```bash
docker-compose logs -f yt-dlp-server
```

---

## 🔍 Troubleshooting

### Issue: Container won't start

**Check logs:**
```bash
docker-compose logs yt-dlp-server
```

**Common causes:**
- Port 3021 already in use
- Insufficient disk space
- Docker daemon not running

**Solution:**
```bash
# Check port usage
lsof -i :3021

# Check disk space
df -h

# Restart Docker daemon
sudo systemctl restart docker
```

---

### Issue: yt-dlp not found

**Check yt-dlp installation:**
```bash
docker-compose exec yt-dlp-server yt-dlp --version
```

**Expected:** Version number (e.g., `2025.12.08`)

**If not found:**
```bash
# Rebuild image
docker-compose build --no-cache
docker-compose up -d
```

---

### Issue: Permission denied on temp directory

**Check permissions:**
```bash
docker-compose exec yt-dlp-server ls -la /app/temp
```

**Fix:**
```bash
docker-compose exec yt-dlp-server chmod 777 /app/temp
```

Or update Dockerfile:
```dockerfile
RUN mkdir -p temp && chmod 777 temp
```

---

### Issue: Health check failing

**Check health endpoint:**
```bash
curl http://localhost:3021/health
```

**Check container status:**
```bash
docker-compose ps
```

**Restart container:**
```bash
docker-compose restart yt-dlp-server
```

---

## 📈 Monitoring

### View Container Stats

```bash
docker stats yt-dlp-server
```

### View Container Logs

```bash
# Follow logs
docker-compose logs -f yt-dlp-server

# Last 100 lines
docker-compose logs --tail=100 yt-dlp-server

# Logs with timestamps
docker-compose logs -t yt-dlp-server
```

### Check Resource Usage

```bash
# CPU and Memory
docker stats yt-dlp-server --no-stream

# Disk usage
docker system df
```

---

## 🔄 Updates

### Update Application Code

```bash
# Stop container
docker-compose down

# Rebuild image
docker-compose build

# Start container
docker-compose up -d
```

### Update yt-dlp

```bash
# Rebuild with --no-cache to force pip reinstall
docker-compose build --no-cache

# Restart
docker-compose up -d
```

---

## 🗑️ Cleanup

### Stop and Remove Containers

```bash
docker-compose down
```

### Remove Volumes

```bash
docker-compose down -v
```

### Remove Images

```bash
docker-compose down --rmi all
```

### Complete Cleanup

```bash
# Stop, remove containers, volumes, and images
docker-compose down -v --rmi all

# Remove temp directory
rm -rf temp
```

---

## 🚀 Production Deployment

### 1. Update Configuration

Edit `docker-compose.yml`:
```yaml
environment:
  - BASE_URL=https://api.yourdomain.com
  - ENCRYPTION_KEY=your-secure-key-here
  - NODE_ENV=production
```

### 2. Use Reverse Proxy

**Nginx example:**
```nginx
server {
    listen 80;
    server_name api.yourdomain.com;
    
    location / {
        proxy_pass http://localhost:3021;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 3. Enable SSL

Use Let's Encrypt with Certbot:
```bash
certbot --nginx -d api.yourdomain.com
```

### 4. Set Up Monitoring

**Health check endpoint:**
```bash
# Add to monitoring system
curl http://localhost:3021/health
```

**Log aggregation:**
```bash
# Use Docker logging driver
docker-compose up -d --log-driver json-file --log-opt max-size=10m
```

---

## 📝 Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3021` | Server port |
| `BASE_URL` | `http://localhost:3021` | Public URL for download links |
| `ENCRYPTION_KEY` | `overflow` | Encryption key for download links |
| `YT_DLP_PATH` | `/usr/local/bin/yt-dlp.sh` | Path to yt-dlp executable |
| `FFMPEG_PATH` | `ffmpeg` | Path to ffmpeg executable |
| `TEMP_DIR` | `./temp` | Temporary files directory |
| `NODE_ENV` | `production` | Node.js environment |

---

## 🔐 Security Best Practices

### 1. Change Default Encryption Key

```yaml
environment:
  - ENCRYPTION_KEY=$(openssl rand -hex 32)
```

### 2. Use Secrets Management

**Docker Secrets (Swarm):**
```yaml
secrets:
  encryption_key:
    external: true

environment:
  - ENCRYPTION_KEY_FILE=/run/secrets/encryption_key
```

**Environment File:**
```bash
# .env.production
ENCRYPTION_KEY=your-secure-key
```

```yaml
env_file:
  - .env.production
```

### 3. Limit Resource Usage

```yaml
deploy:
  resources:
    limits:
      cpus: '2'
      memory: 2G
    reservations:
      cpus: '1'
      memory: 1G
```

### 4. Use Read-Only Root Filesystem

```yaml
read_only: true
tmpfs:
  - /tmp
  - /app/temp
```

---

## 🎯 Integration with Frontend

### Update Frontend .env

```env
API_URL=http://localhost:3021
```

### Or Use Docker Network

If frontend also runs in Docker:

```yaml
# docker-compose.yml (frontend)
services:
  frontend:
    environment:
      - API_URL=http://yt-dlp-server:3021
    networks:
      - tiktok_network
```

---

## 📚 Additional Resources

- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [yt-dlp Documentation](https://github.com/yt-dlp/yt-dlp)
- [Node.js Docker Best Practices](https://github.com/nodejs/docker-node/blob/main/docs/BestPractices.md)

---

## ✅ Checklist

Before deploying to production:

- [ ] Change `BASE_URL` to production URL
- [ ] Change `ENCRYPTION_KEY` to secure key
- [ ] Set up reverse proxy (Nginx/Caddy)
- [ ] Enable SSL/TLS
- [ ] Set up monitoring
- [ ] Configure log rotation
- [ ] Set resource limits
- [ ] Test health checks
- [ ] Test all endpoints
- [ ] Set up backups (if needed)

---

## 🎉 Summary

**Docker setup complete!**

- ✅ Dockerfile created
- ✅ docker-compose.yml created
- ✅ .dockerignore created
- ✅ Health checks configured
- ✅ Volume mounts configured
- ✅ Production ready

**Start server:**
```bash
docker-compose up -d
```

**Test:**
```bash
curl http://localhost:3021/health
```

**Ready for production!** 🚀
