# tiktok-api-dl-server

Deno API server untuk **fetch** dan **download** post TikTok (video / image / music / **slideshow**) memakai package [`tiktok-api-dl`](https://github.com/almafazi/tiktok-api-dl) (fork `almafazi/tiktok-api-dl` @ v1.3.8).

Response JSON dan endpoint path **disamakan** dengan `yt-dlp-wireproxy` (Go gateway di port 9111).

## Requirement

- [Deno](https://deno.com) >= 2.0
- [ffmpeg](https://ffmpeg.org) (diperlukan untuk rendering slideshow photo post → MP4)

## Install & Run

```bash
cd tiktok-api-dl
# GitHub deps require bun/npm (deno install cannot resolve github: specs)
bun install --frozen-lockfile
cp .env.example .env   # opsional
deno task start        # atau: deno task dev (watch mode)
```

Server berjalan di `http://localhost:7788`.

## Endpoints

| Method | Path | Auth | Fungsi |
|---|---|---|---|
| `GET` | `/` | — | Status service |
| `GET` | `/health` | — | Health check |
| `POST` | `/tiktok` | `X-API-Key` (jika `TIKTOK_API_KEY` di-set) | Ekstrak metadata + generate encrypted download links |
| `GET` | `/tiktok/download` | — | Stream video / image / mp3 / slideshow via `key` |

### `POST /tiktok`

Body:
```json
{
  "url": "https://www.tiktok.com/@user/video/<id>",
  "version": "v1",          // opsional, default v1 (v1|v2|v3)
  "proxy": "http://...",    // opsional
  "impersonate": "chrome"   // opsional
}
```

**Response (video post — `status: "tunnel"`):**
```json
{
  "status": "tunnel",
  "extract_source": "web",
  "title": "...",
  "description": "...",
  "statistics": {
    "play_count": 0,
    "digg_count": 0,
    "comment_count": 0,
    "share_count": 0
  },
  "artist": "nickname",
  "cover": "https://...",
  "duration": 10053,
  "audio": "https://...tiktokcdn....mp3",
  "download_link": {
    "watermark": "/tiktok/download?key=...",
    "no_watermark": "/tiktok/download?key=...",
    "no_watermark_hd": "/tiktok/download?key=...",
    "mp3": "/tiktok/download?key=..."
  },
  "music_duration": 10053,
  "author": {
    "nickname": "...",
    "uniqueId": "username",
    "signature": "",
    "avatar": "https://...",
    "avatarThumb": "https://...",
    "avatarMedium": "https://...",
    "avatarLarger": "https://..."
  }
}
```

**Response (photo/slideshow post — `status: "picker"`):**
```json
{
  "status": "picker",
  "extract_source": "web",
  "title": "...",
  "description": "...",
  "statistics": { "play_count": 0, "digg_count": 0, "comment_count": 0, "share_count": 0 },
  "artist": "nickname",
  "cover": "https://...",
  "duration": 0,
  "audio": "https://...mp3",
  "download_link": {
    "no_watermark": ["/tiktok/download?key=...", "/tiktok/download?key=..."],
    "mp3": "/tiktok/download?key=..."
  },
  "photos": [
    { "type": "photo", "url": "https://...", "download_link": "/tiktok/download?key=..." },
    { "type": "photo", "url": "https://...", "download_link": "/tiktok/download?key=..." }
  ],
  "download_slideshow": "/tiktok/download?key=...",
  "author": { "nickname": "...", "uniqueId": "...", ... }
}
```

### `GET /tiktok/download`

| Param | Wajib | Default | Keterangan |
|---|---|---|---|
| `key` | ya | — | Download key dari response `/tiktok` |
| `download` | tidak | `true` | `true` = attachment, `false` = inline |

Tipe konten ditentukan oleh session type:
- `video` → `video/mp4`, filename `{author}_{quality}.mp4`
- `photo` → `image/jpeg`, filename `{author}_photo_{index}.jpg`
- `mp3` → `audio/mpeg`, filename `{author}.mp3`
- `slideshow` → `video/mp4` (hasil ffmpeg), filename `{author}_slideshow.mp4`

Session key berlaku **5 menit** (300 detik), disimpan di **Redis** (jika `REDIS_URL` di-set) atau in-memory fallback. Download memakai **atomic claim** (`GETDEL`/get-and-delete): hanya satu consumer yang dapat key. Jika delivery gagal sebelum media terkirim (CDN error, ffmpeg error, client abort sebelum headers), session di-**restore** dengan sisa TTL agar retry tetap bisa.

### Session store & Redis

- **Docker (dev/prod):** otomatis pakai Redis (`redis://redis:6379/0`), session persist saat container restart.
- **Local tanpa docker:** biarkan `REDIS_URL` kosong → pakai in-memory Map (session hilang saat server restart).
- Redis key prefix: `tiktok:session:`, TTL 300 detik, policy `allkeys-lru` (maxmemory 128MB).
- Endpoint `/health` menampilkan `session_backend: "redis"` atau `"memory"` dan `active_sessions` count.

### Extraction cache (`POST /tiktok`)

Hasil ekstraksi upstream `Tiktok.Downloader()` di-**cache** (Redis jika `REDIS_URL` di-set, otherwise in-memory) agar request berulang ke URL yang sama tidak memanggil upstream TikTok berkali-kali. Ini menurunkan latensi & risiko rate-limit, sama seperti `extraction_cache` di `yt-dlp-wireproxy`.

- **Yang di-cache:** raw extraction result (metadata + CDN URL), **bukan** response final. Download links (`/tiktok/download?key=...`) tetap dibuat fresh per request → session baru setiap kali.
- **Cache key:** `SHA-256(url|proxy|impersonate|version)`, prefix Redis `exinfo:`.
- **TTL:** `TIKTOK_EXTRACT_CACHE_TTL_SECONDS` (default **1800 detik / 30 menit** — CDN URL TikTok berlaku ~6 jam, 30 menit batas aman menghindari stale URL & statistik terlalu tua).
- **Stampede protection:** Redis `SET NX` lock (TTL 35s); request concurrent ke URL sama menunggu hingga 8 detik lalu fallback ke ekstraksi sendiri.
- **Graceful degradation:** jika Redis down, otomatis fall-through ke ekstraksi langsung tanpa cache.
- Endpoint `/health` menampilkan `extract_cache_backend` (`redis`/`memory`) dan `extract_cache_ttl_seconds`.

### Slideshow rendering

Saat `key` merujuk ke session type `slideshow`:
1. Download semua gambar + audio dari CDN TikTok (stream ke disk, bukan full buffer)
2. Render via `ffmpeg`:
   - Setiap gambar ditampilkan 4 detik
   - Scale ke 720x1280 (portrait), pad hitam
   - 24 fps, libx264 `ultrafast` `stillimage` CRF 28
   - Audio aac 128k (jika ada)
3. Stream file MP4 ke klien (`createReadStream` + `Content-Length`), lalu hapus temp dir di `finally`

## Test

```bash
./test.sh
```

Menguji `/tiktok` (POST) dan `/tiktok/download` (GET) untuk:
- 2 link video
- 1 link photo/slideshow (render MP4 via ffmpeg)

## Struktur

```
tiktok-api-dl/
├── package.json
├── deno.json
├── .env.example
├── Dockerfile
├── docker-compose.yml    # dev & prod profile + redis service + mem_limit
├── src/
│   ├── index.ts       # node:http: CORS + routing / /health /tiktok /tiktok/download
│   ├── tiktok.ts      # wrapper Tiktok.Downloader + wireproxy-compatible JSON + key generation
│   ├── extraction_cache.ts # Redis/in-memory cache hasil ekstraksi (TTL + stampede protection)
│   ├── session.ts     # session store: Redis backend + in-memory fallback (TTL 5 menit)
│   └── slideshow.ts   # ffmpeg slideshow rendering (photo post → MP4)
└── test.sh
```

## Environment Variables

| Var | Default | Keterangan |
|---|---|---|
| `PORT` | `7788` | Port server |
| `DEFAULT_VERSION` | `v1` | Default downloader version (`v1`/`v2`/`v3`) |
| `TIKTOK_API_KEY` | (kosong) | Jika di-set, endpoint `/tiktok` butuh header `X-API-Key` |
| `FFMPEG_PATH` | `ffmpeg` | Path ke binary ffmpeg |
| `REDIS_URL` | (kosong) | Jika di-set, session store & extraction cache pakai Redis; jika kosong, pakai in-memory fallback |
| `TIKTOK_EXTRACT_CACHE_TTL_SECONDS` | `1800` | TTL cache hasil ekstraksi `/tiktok` (detik, 30 menit) |
| `MAX_BODY_BYTES` | `65536` | Batas ukuran body POST (anti unbounded allocation) |
| `SLIDESHOW_MAX_CONCURRENT` | `1` | Max render slideshow paralel (cegah OOM di mem_limit) |
| `SLIDESHOW_MAX_PHOTOS` | `35` | Max foto per slideshow |
| `SLIDESHOW_MAX_FILE_BYTES` | `15728640` | Max size per asset download (15MB) |
| `SLIDESHOW_MAX_TOTAL_BYTES` | `83886080` | Max total download budget per slideshow (80MB) |

## Docker

Dockerized dengan 2 environment: **dev** dan **prod**. Kredensial `TIKTOK_API_KEY` disamakan dengan `yt-dlp-wireproxy`.

### File env

| File | Environment | `TIKTOK_API_KEY` |
|---|---|---|
| `.env` | dev | `test` (sama dengan wireproxy `.env`) |
| `.env.prod` | prod | `aluzsMZWWlr7sqomlf20BAUmbfZCJb` (sama dengan wireproxy `.env.example`) |

### Development (hot-reload)

```bash
# Build & start dev container (watch mode, source di-mount sebagai volume)
docker compose --profile dev up --build

# Atau detached
docker compose --profile dev up -d --build

# Lihat logs
docker compose logs -f tiktok-api-dev

# Stop
docker compose --profile dev down
```

Dev container:
- Build target `development` dari Dockerfile (Deno `--watch`)
- Source `./src` di-mount read-only → hot reload tanpa rebuild
- Port `7788` di-expose
- `mem_limit: 512m`
- `TIKTOK_API_KEY=test` (dari `.env`)

### Production

```bash
# Build & start prod container
docker compose --profile prod up -d --build

# Lihat logs
docker compose logs -f tiktok-api-prod

# Stop
docker compose --profile prod down
```

Prod container:
- Build target `production` dari Dockerfile (Deno)
- Image `tiktok-api-dl:prod` (self-contained, tidak ada volume mount)
- Port `7788` di-expose
- `mem_limit: 512m` + `SLIDESHOW_MAX_CONCURRENT=1` (OOM guard + serial ffmpeg)
- `TIKTOK_API_KEY=aluzsMZWWlr7sqomlf20BAUmbfZCJb` (dari `.env.prod`)
- Health check via `curl /health` setiap 30 detik (`rss_mb` / `heap_used_mb` di response)

### Test di dalam Docker

```bash
# Setelah container running (dev atau prod), test dari host:
curl http://localhost:7788/health

# POST /tiktok (dev: pakai API key "test")
curl -X POST http://localhost:7788/tiktok \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test" \
  -d '{"url":"https://www.tiktok.com/@arctic.motion/video/7644267480856136991"}'

# POST /tiktok (prod: pakai API key dari .env.prod)
curl -X POST http://localhost:7788/tiktok \
  -H "Content-Type: application/json" \
  -H "X-API-Key: aluzsMZWWlr7sqomlf20BAUmbfZCJb" \
  -d '{"url":"https://www.tiktok.com/@arctic.motion/video/7644267480856136991"}'
```

### Dockerfile stages

```
base         → denoland/deno + ffmpeg + npm deps (node_modules) + src
├── development  → deno run --watch (hot reload, volume mount)
└── production   → deno run src/index.ts (self-contained)
```
