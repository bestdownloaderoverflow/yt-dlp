# Error Mapping and Gateway Actions

Dokumen ini menjelaskan mapping final dari error `yt_dlp` di worker API ke status HTTP, dan aksi yang dilakukan oleh `gateway-go`.

## Worker Mapping (`yt-dlp-stream` API)

| Source Exception | Signal | Worker HTTP Status | Worker Payload | Catatan |
|---|---|---|---|---|
| `yt_dlp` network cause `HTTPError(status=429)` | status dari exception chain | `429` | `{"error":"RATE_LIMITED","retry_after":300,...}` + `Retry-After: 300` | Jalur rate-limit utama |
| `yt_dlp` network cause `HTTPError(status=403)` | status dari exception chain | `403` | message/forbidden detail | Biasanya private/blocked/forbidden upstream |
| `yt_dlp` network cause `HTTPError(status=404)` | status dari exception chain | `404` | not-found detail | URL konten tidak ditemukan |
| `yt_dlp` `DownloadError`/`ExtractorError` dengan pesan rate-limit | pattern message | `429` | `RATE_LIMITED` + `retry_after` | Fallback saat status tidak tersurface |
| `yt_dlp` `DownloadError` non-rate-limit | default mapper | `400` | detail string error | Request/content problem |
| Generic exception non-rate-limit | default mapper | `500` | internal error detail | Internal processing error |

## Gateway Mapping (`gateway-go`)

| Worker HTTP Status | Gateway Behavior | Restart Container | Failover |
|---|---|---|---|
| `200` | Proxy response ke client | Tidak | Tidak perlu |
| `403` | Dianggap forbidden/rate path | Ya (scheduled) | Ya |
| `429` | Dianggap rate-limit path | Ya (scheduled) | Ya |
| `400` | Retryable failover tanpa restart | Tidak | Ya |
| `4xx` lain | Return as-is ke client | Tidak | Tidak |
| `5xx` / network error | Retry failover | Tidak otomatis | Ya |

## Circuit Breaker + Drain (Restart Flow)

Saat worker masuk restart flow (`403/429`):

1. Worker ditandai `breaker_open=true` (tidak menerima request baru).
2. Gateway menunggu request aktif selesai (`active_requests == 0`).
3. Jika drain selesai dalam timeout, restart container dijalankan.
4. Jika timeout habis, restart tetap dipaksa.
5. Setelah restart sukses + health check, circuit ditutup (`breaker_open=false`).

## Environment Knobs (Gateway)

- `DRAIN_TIMEOUT_SECONDS` (default `90`)
- `DRAIN_POLL_INTERVAL_MS` (default `500`)
- `RESTART_BACKOFF_BASE`, `RESTART_BACKOFF_MAX`, `RESTART_BUDGET_LIMIT`, `RESTART_BUDGET_WINDOW`, `RESTART_QUARANTINE_SECONDS`

## Referensi Kode

- Worker mapper: `core/error_mapping.py`
- Worker API handlers:
  - `api/fetch.py`
  - `api/stream_ffmpeg.py`
  - `api/stream_chunked.py`
  - `api/stream_direct.py`
  - `api/tiktok.py`
- Gateway policy & breaker/drain:
  - `gateway-go/main.go`

