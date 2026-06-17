# tiktok-api-dl-server

Bun API server untuk **fetch** dan **download** post TikTok (video / image / music) memakai package [`tiktok-api-dl`](https://github.com/almafazi/tiktok-api-dl) (fork `almafazi/tiktok-api-dl` @ v1.3.8).

## Requirement

- [Bun](https://bun.sh) >= 1.2

## Install & Run

```bash
cd tiktok-api-dl
bun install
cp .env.example .env   # opsional, default PORT=7788
bun run start          # atau: bun run dev (watch mode)
```

Server berjalan di `http://localhost:7788`.

## Endpoints

### `GET /health`
Cek status server.

### `GET|POST /fetch`
Ambil metadata post + URL media (JSON).

| Param | Lokasi | Wajib | Default | Keterangan |
|---|---|---|---|---|
| `url` | query / body | ya | — | URL post TikTok |
| `version` | query / body | tidak | `v1` | `v1` (Tiktok API) / `v2` (SSSTik) / `v3` (MusicalDown) |

Contoh:
```bash
curl -G http://localhost:7788/fetch \
  --data-urlencode "url=https://www.tiktok.com/@arctic.motion/video/7644267480856136991"
```

Response (ringkas):
```json
{
  "status": "success",
  "version": "v1",
  "type": "video",
  "id": "7644267480856136991",
  "description": "...",
  "author": { "username": "arctic.motion", "nickname": "...", "avatar": "..." },
  "statistics": { "playCount": 123, "likeCount": 45, ... },
  "download": {
    "video": [{ "url": "https://..." }],
    "images": [],
    "music":  [{ "url": "https://...", "title": "...", "author": "..." }]
  },
  "raw": { ... }
}
```

### `GET /download`
Stream langsung file media dari CDN TikTok ke klien (header `Content-Disposition: attachment`).

| Param | Wajib | Default | Keterangan |
|---|---|---|---|
| `url` | ya | — | URL post TikTok |
| `type` | tidak | `video` | `video` / `image` / `music` |
| `index` | tidak | `0` | Index gambar untuk post slide (`type=image`) |
| `version` | tidak | `v1` | `v1` / `v2` / `v3` |

Contoh:
```bash
# video
curl -G http://localhost:7788/download \
  --data-urlencode "url=https://www.tiktok.com/@arctic.motion/video/7644267480856136991" \
  --data-urlencode "type=video" -o video.mp4

# gambar ke-2 dari post slide
curl -G http://localhost:7788/download \
  --data-urlencode "url=<URL>" --data-urlencode "type=image" --data-urlencode "index=1" -o img.jpg

# audio/music
curl -G http://localhost:7788/download \
  --data-urlencode "url=<URL>" --data-urlencode "type=music" -o audio.mp3
```

## Test

```bash
./test.sh
```

Menguji `/health`, `/fetch`, dan `/download` untuk dua link TikTok.

## Struktur

```
tiktok-api-dl/
├── package.json      # dependency: @tobyg74/tiktok-api-dl @ github:almafazi/tiktok-api-dl#master
├── .env.example      # PORT=7788, DEFAULT_VERSION=v1
├── src/
│   ├── index.ts      # Bun.serve: CORS + routing /health /fetch /download
│   └── tiktok.ts     # wrapper Tiktok.Downloader + helper media URL/filename
└── test.sh
```
