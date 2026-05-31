# Go + Python Extractor Migration Plan

Dokumen ini menjelaskan migrasi dari `FastAPI + Granian` ke `Go gateway + Python extractor daemon`.

## Target Arsitektur (Final - Done)

- Go menjadi HTTP gateway utama.
- Python hanya menjalankan fungsi extractor yt-dlp (stateful cookiejar/session).
- Komunikasi Go -> Python menggunakan IPC (TCP JSON-RPC per worker).

## Status Implementasi

- [x] Daemon Python extractor: `extractor/worker_daemon.py` + TCP server wrapper `extractor/worker_server.py`.
- [x] IPC pool di Go gateway.
- [x] Endpoint berikut sudah full lewat Go + IPC:
  - `GET /info`
  - `GET /fetch`
  - `GET /download`
  - `GET /stream/video`
  - `GET /stream/video-chunked`
  - `GET /stream/mp3`
  - `GET /stream/mp3-chunked`
  - `GET /stream/m4a`
  - `POST /tiktok`
  - `GET /tiktok/download`
- [x] Go menangani failover/retry/restart policy + ffmpeg merge/transcode untuk delivery path yang membutuhkan.
- [x] Dependensi worker HTTP FastAPI/Granian untuk jalur publik telah dicabut.

## Prinsip Penting

- Satu Python process = satu cookie/session context.
- Jangan spawn Python per request; selalu pre-spawn dan reuse process.
- Gunakan sticky worker selection untuk request yang harus konsisten session.
