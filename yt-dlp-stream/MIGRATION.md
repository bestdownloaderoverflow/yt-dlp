# Migrasi gateway-go: Monolith → Multi-Package

## Latar Belakang

Gateway Go (`gateway-go/main.go`) awalnya adalah file monolitik 3301 baris yang menangani **seluruh** aspek server: routing, worker management, VPN rotation, rate limiting, streaming orchestration, ffmpeg piping, chunked HTTP range requests, TikTok slideshow, dan proxy failover — semua dalam satu file.

### Masalah
- **3301 baris dalam satu file** (`main.go`) — sulit dibaca, dicari, dan di-maintain
- **Tidak ada pemisahan domain** — handler HTTP, delivery engine, worker registry, konfigurasi tercampur
- **TikTok vs YouTube logic bercampur** — keduanya pakai `streamDirectPlanWithRefresh`/`streamWithFFmpegFromPlan` tanpa pembeda platform
- **Sulit testing** — satu file berisi 60+ fungsi semua bergantung pada satu `Gateway` struct
- **Sulit onboard developer baru** — tidak ada entry point yang jelas

### Tujuan Refactor
1. **Pecah ke sub-package terstruktur** — `handlers/`, `delivery/`, `registry/`, `utils/`
2. **Go menangani semua streaming & delivery** — IPC ke Python worker **HANYA** untuk extraction/metadata
3. **Behavior tidak berubah** — semua route, response contract, retry logic identik dengan original
4. **Pisahkan platform logic** — TikTok punya 403 body check, YouTube punya chunked range, Twitter direct proxy

---

## Sebelum vs Sesudah

### Sebelum
```
gateway-go/
  main.go                3301 lines — SEMUA ADA DI SINI
  extractor_ipc.go         236 lines — TCP JSON-RPC ke Python
  go.mod                     3 lines
```

### Sesudah
```
gateway-go/
  main.go                  143 lines — Bootstrap: wire components + start server
  extractor_ipc.go         231 lines — TCP JSON-RPC ke Python (unchanged protocol)

  registry/
    worker.go              411 lines — Worker, WorkerRegistry (state machine)
    rotator.go             178 lines — VPNRotator (drain → restart → health check)

  utils/
    config.go              136 lines — Environment variables + defaults
    rate_limiter.go         49 lines — SlidingWindowRateLimiter

  handlers/
    router.go              734 lines — HTTP mux, proxy logic, retry/rotation, goroutines
    handlers.go            552 lines — Route handlers: fetch, download, stream, tiktok, tunnel

  delivery/
    plan.go                341 lines — DeliveryPlan, ParseDeliveryPlan, utilities
    delivery.go            119 lines — Delivery struct, HTTP client factory
    direct.go              156 lines — Direct pass-through streaming (TikTok 403 body check)
    chunked.go             415 lines — HTTP Range chunked streaming (10MB/8MB chunks)
    ffmpeg.go              213 lines — FFmpeg piping (audio mp3, merge AV, transcode)
    tiktok_slideshow.go    229 lines — TikTok photo slideshow → ffmpeg concat → MP4
```

| Metric | Sebelum | Sesudah |
|--------|---------|---------|
| Total baris kode | 3537 | ~3169 |
| Jumlah file | 3 | 14 |
| Package | 1 (`main`) | 5 (`main`, `registry`, `utils`, `handlers`, `delivery`) |
| File terbesar | 3301 lines | 734 lines |

---

## Arsitektur

### Dependency Graph

```
main.go
  ├── utils.Config        (load env vars)
  ├── registry            (WorkerRegistry, VPNRotator)
  ├── delivery.Delivery   (streaming engine)
  ├── handlers.Handlers   (HTTP routing + proxy)
  └── ExtractorPool       (IPC to Python workers)

handlers/
  ├── utils.Config
  ├── registry.WorkerRegistry
  ├── delivery.Delivery
  └── Extractor (interface)

delivery/
  ├── utils (config subset via DeliveryConfig)
  └── Extractor (interface minimal untuk refresh)

registry/
  └── (tidak depend internal packages)
```

### Package Responsibilities

#### `utils/` — Shared utilities
**`config.go`** — Baca environment variables, convert ke typed `Config` struct.
```go
type Config struct {
    GatewayPort               int
    WorkerCount               int
    ExtractorIPCEnabled       bool
    ExtractorTimeoutMs        int
    GluetunPassword           string
    MaxRetries                int
    HealthCheckTimeoutMs      int
    HealthMonitorIntervalMs   int
    HealthFailureThreshold    int
    RateLimitCooldownSeconds  int
    RestartBackoffBase        int
    RestartBackoffMax         int
    RestartBudgetLimit        int
    RestartBudgetWindow       int
    RestartQuarantineSeconds  int
    RestartBackoffJitter      int
    DegradedRetryAfter        int
    GatewayRLWindowSeconds    int
    GatewayRLFetchLimit       int
    GatewayRLDownloadLimit    int
    DrainTimeoutSeconds       int
    DrainPollIntervalMs       int
    RestartStabilizeSeconds   int
    UnhealthyRestartThreshold int
}

func LoadConfig() Config  // baca os.Getenv dengan fallback
func ParseBoolQuery(raw, fallback) bool
func ExtractWorkerID(key, workerCount) string  // parse "w1::uuid" → "w1"
```

**`rate_limiter.go`** — Sliding window rate limiter per-IP.
```go
type SlidingWindowRateLimiter struct { ... }
func NewSlidingWindowRateLimiter(limit, windowSeconds int) *SlidingWindowRateLimiter
func (l *SlidingWindowRateLimiter) Check(key string) (allowed bool, retryAfter int)
```

#### `registry/` — Worker management & VPN rotation
**`worker.go`** — State machine 14 field untuk setiap worker.
```go
type Worker struct {
    ID, Host            string
    APIPort, ProxyPort  int
    Healthy             bool   // probe berhasil?
    Restarting          bool   // sedang restart?
    RestartScheduled    bool   // sudah dijadwalkan?
    Failures            int    // total
    RestartFailures     int    // consecutive restart failure
    ProbeFailures       int    // consecutive health probe failure
    StartedAt           time.Time
    ActiveRequests      int    // concurrent requests
    LastRateLimit       time.Time
    NextRestartAt       time.Time
    QuarantineUntil     time.Time
    RestartEvents       []time.Time  // untuk sliding restart budget
    BreakerOpen         bool
}
```

**`rotator.go`** — Restart container via Docker CLI.
```go
// Alur drain → restart → health check
func (v *VPNRotator) RestartWorker(ctx context.Context, workerID string) bool {
    v.WaitForDrain(ctx, workerID)            // tunggu activeRequests == 0 (timeout 90s)
    docker restart ytdlp-gluetun-N           // restart VPN (wait 10s)
    docker restart ytdlp-stream-N            // restart worker (wait 5s)
    for i := 0; i < 6 {                      // health check loop (5s interval)
        v.HealthCheck(workerID)              // TCP dial port + HTTP /v1/publicip/ip
    }
    v.MarkRestarted(workerID, healthy)
}
```

#### `delivery/` — Streaming engine (di Go, bukan Python)

**`plan.go`** — Typed struct + parsing dari IPC `map[string]any`.
```go
type DeliveryMode string
const (
    ModeDirect  DeliveryMode = "direct"   // pass-through HTTP upstream
    ModeChunked DeliveryMode = "chunked"  // HTTP Range request, known size
    ModeFFmpeg  DeliveryMode = "ffmpeg"   // pipe via ffmpeg transcode/merge
)

type DeliveryPlan struct {
    DirectURL         string
    RequestHeaders    map[string]string
    ResponseHeaders   map[string]string
    MediaType         string
    CanRefresh        bool
    NeedsFFmpeg       bool
    MergeAV           bool           // video + audio streams?
    FFmpegAudioURL    string
    FFmpegAudioHdrs   map[string]string
    FFmpegAudioOnly   bool           // MP3 transcoding?
    Platform          string         // "youtube", "tiktok", "twitter", ...
    SessionType       string         // "video", "audio", "mp3"
    DeliveryMode      string         // dari Python: "single_progressive", "multi_progressive", "ffmpeg"
    PhotoURLs         []string       // TikTok slideshow
    AudioURL          string         // TikTok slideshow audio
    DurationPerImage  int            // detik per image slideshow
    ContentType       string         // "slideshow" untuk TikTok photo
    FallbackProxy     bool           // apakah proxy fallback diperlukan?
    Key               string         // session key untuk refresh
}

func ParseDeliveryPlan(map[string]any) DeliveryPlan
func ResolveDeliveryMode(plan DeliveryPlan, route string) DeliveryMode
func PickStreamDownloadKey(fetchResult map[string]any, path, quality string) string

// Utilities
func ShouldRefreshTikTokForbidden(body []byte) bool  // cek permanent vs transient error
```

**`delivery.go`** — Delivery struct, HTTP client factory.
```go
type Delivery struct {
    Config  DeliveryConfig     // DegradedRetryAfter
    Worker  WorkerLookup       // interface: GetWorker(id) *Worker
    BaseCl  *http.Client       // base client tanpa proxy
}

// mediaHTTPClient()     — tanpa proxy
// mediaHTTPClientForPlan() — pilih proxy worker ATAU bypass (TikTok media)
```

**`direct.go`** — Direct pass-through streaming.
```go
func (d *Delivery) StreamDirect(w, r, workerID, plan, onRefresh) bool {
    // 1. Forward Range/If-Range header ke upstream
    // 2. Stream response body ke client
    // 3. Jika 403 DAN TikTok: cek body untuk permanent error
    //    - geo-restrict, captcha, login → TIDAK refresh, forward body ke client
    //    - body kosong/transient → refresh URL via IPC, retry sekali
    // 4. Jika 403 DAN bukan TikTok: blind refresh sekali
}
```

**`chunked.go`** — HTTP Range chunked streaming (gaya Cobalt).
```go
func (d *Delivery) StreamChunked(w, r, workerID, plan, onRefresh, chunkSize) bool {
    // 1. Baca Content-Length dari ResponseHeaders
    // 2. Jika tidak ada → fallback ke StreamDirect
    // 3. Loop Range request: bytes=N-10MB → bytes=N+10MB-1 → ...
    // 4. Jika 403 → refresh URL, continue
    // 5. Response 206 partial content → copy ke client
}

func (d *Delivery) DownloadToFile(ctx, dstPath, plan, selector, chunkSize, onRefresh) (DeliveryPlan, error)
// Download ke file lokal (untuk slideshow, ffmpeg merge pipe)

func (d *Delivery) DownloadToWriter(ctx, dst, plan, selector, chunkSize, onRefresh) error
// Download ke io.WriteCloser (untuk pipe ke ffmpeg stdin)
```

**`ffmpeg.go`** — FFmpeg piping untuk 3 kasus:
```go
func (d *Delivery) StreamFFmpeg(w, r, workerID, plan, onRefresh) bool {
    // Kasus 1: FFmpegAudioOnly = true
    //   Download primary source → pipe → ffmpeg → libmp3lame 192k → pipe:1 → client

    // Kasus 2: MergeAV = true (DASH video + audio terpisah)
    //   Download video → pipe stdin
    //   Download audio → pipe fd 3
    //   ffmpeg -i pipe:0 -i pipe:3 -c:v copy -c:a aac -f mp4 → pipe:1 → client

    // Kasus 3: Direct URL copy
    //   ffmpeg -i URL -c copy -f mp4 → pipe:1 → client
}
```

**`tiktok_slideshow.go`** — Render slideshow dari foto ke MP4.
```go
func (d *Delivery) StreamTikTokSlideshow(w, r, plan, onRefresh) {
    // 1. Download semua foto JPG ke temp dir
    // 2. Download audio MP3 (jika ada)
    // 3. ffmpeg concat demuxer:
    //    - scale 720x1280, pad to fill, 4s per image, FPS 24
    //    - loop audio, trim ke total durasi video
    //    - libx264 medium CRF 23 yuv420p, AAC 128k
    // 4. Stream output MP4 ke client
    // 5. Jika gagal → refresh plan → retry sekali
}
```

#### `handlers/` — HTTP request handlers

**`router.go`** — Core mux, proxy logic, worker management.
```go
type Handlers struct {
    Config     utils.Config
    Registry   *registry.WorkerRegistry
    Rotator    *registry.VPNRotator
    Extractor  Extractor       // interface ke IPC
    Delivery   *delivery.Delivery
    Client     *http.Client
    RLFetch    *utils.SlidingWindowRateLimiter   // 45 req/60s
    RLDownload *utils.SlidingWindowRateLimiter   // 45 req/60s
}

// ServeHTTP dispatch semua route:
//   "/"              → handleRoot
//   "/fetch"         → handleFetch (rate-limited)
//   "/download"      → handleDownload (rate-limited)
//   "/info"          → handleInfo
//   "/stream/*"      → handleStream
//   "/tiktok"        → handleTikTok (POST)
//   "/tiktok/download" → handleTikTokDownload
//   "/health"        → handleHealth
//   "/tunnel"        → handleTunnel
//
// Proxy methods:
//   proxyWithRetry()      — retry ke healthy worker, queue restart
//   proxyWithRotation()   — rotate worker per attempt
//   proxyStreamingResponse() — single attempt proxy dengan response analysis
//   handleResponse()      — analyze 4xx/5xx body for geo-block, rate-limit, captcha
//
// Goroutines:
//   RestartScheduler()  — tick 10s, restart scheduled workers
//   UptimeChecker()     — tick 60s, restart workers >= 24h idle
//   HealthMonitor()     — tick 5s, TCP probe + HTTP health check
```

**`handlers.go`** — Route handler implementations.
```go
// handleFetchViaExtractorIPC() → Extractor.Fetch(workerID, url, proxy, impersonate)
// handleDownloadViaExtractorIPC() → Extractor.DownloadPrepare() → Delivery.StreamChunked/FFmpeg/Direct
// handleStreamViaExtractorIPC() → Extractor.Fetch() → PickStreamDownloadKey() → DownloadPrepare()
// handleTikTokViaExtractorIPC() → Extractor.TikTok(workerID, url, ...)
// handleTikTokDownloadViaExtractorIPC() → Extractor.TikTokDownloadPrepare() → Delivery
// handleTunnel() → streamFromWorker()
```

#### `main.go` — Bootstrap (143 baris)

Tidak ada business logic. Hanya wiring:
```go
func main() {
    cfg := utils.LoadConfig()
    client := &http.Client{Transport: ...}
    reg := registry.NewWorkerRegistry(...)
    rotator := registry.NewVPNRotator(...)

    var ext *ExtractorPool
    if cfg.ExtractorIPCEnabled {
        ext = NewExtractorPool(cfg.WorkerCount, timeout)
    }

    del := delivery.New(..., client, deliveryWorkerLookup{reg})
    h := handlers.New(cfg, reg, rotator, wrapExtractor(ext), del, client)
    defer h.Shutdown()

    go h.RestartScheduler()
    go h.UptimeChecker()
    go h.HealthMonitor()

    http.ListenAndServe(addr, h)
}
```

**Adapters untuk bridge IPC tuples:**
- `deliveryWorkerLookup` — `registry.WorkerRegistry` → `delivery.WorkerLookup` interface
- `extractorAdapter` — `*ExtractorPool` → `handlers.Extractor` interface, convert `*ipcError` → `*handlers.IPCError`

---

## Protocol: IPC ke Python Worker

Tidak berubah. Tetap TCP JSON-RPC newline-delimited:

**Request:**
```json
{"id":"w1-42","method":"fetch","params":{"url":"https://...","impersonate":"chrome"}}
```

**Response (success):**
```json
{"id":"w1-42","ok":true,"result":{"type":"video","platform":"youtube",...}}
```

**Response (error):**
```json
{"id":"w1-42","ok":false,"error":{"code":"geo_restricted","message":"...","status":403}}
```

### RPC Methods
| Method | Dipakai oleh | Fungsi |
|--------|-------------|--------|
| `extract_info` | `/info` | Raw metadata extraction |
| `fetch` | `/fetch`, `/stream/*` | Metadata + quality links + encryption |
| `tiktok` | `POST /tiktok` | TikTok-specific extraction (watermark, author, stats) |
| `tiktok_download_prepare` | `/tiktok/download` | Get session: direct_url, headers, cookies |
| `tiktok_download_refresh` | `/tiktok/download` | Refresh expired TikTok CDN URL |
| `download_prepare` | `/download`, `/stream/*` | Get delivery plan: direct_url, needs_ffmpeg, delivery_mode |
| `download_refresh` | `/download`, `/stream/*` | Refresh expired download URL |
| `resolve_formats` | `/stream/m4a` | Resolve best audio format |
| `health` | Worker health check | Worker liveness probe |

---

## Perbedaan Behavior: Per Platform

### TikTok (`/tiktok`, `/tiktok/download`)
- **POST /tiktok** → `Extractor.TikTok()` → return JSON (author, stats, watermark tiers, download links)
- **GET /tiktok/download** → `TikTokDownloadPrepare()` → dapat direct_url + cookies
- **403 handling**: Cek response body dengan `ShouldRefreshTikTokForbidden()`:
  - **Permanent** (geo_restricted, captcha, login, access denied) → **TIDAK refresh**, langsung forward body ke client
  - **Transient** (body kosong/error umum) → **refresh URL** via `TikTokDownloadRefresh()`, retry sekali
- **Slideshow**: ContentType = "slideshow" → `Delivery.StreamTikTokSlideshow()` → download foto → ffmpeg concat → stream MP4

### YouTube / Umum (`/fetch`, `/download`, `/stream/*`)
- **GET /fetch** → `Extractor.Fetch()` → metadata + download links terenkripsi (Redis session)
- **GET /download** → `Extractor.DownloadPrepare()` → plan dari Python:
  - `needs_ffmpeg=true` → `Delivery.StreamFFmpeg()`
  - `delivery_mode="single_progressive"` + video → `Delivery.StreamChunked()` (10MB Range requests)
  - default → `Delivery.StreamDirect()` (direct pass-through)
- **GET /stream/video-chunked** → Fetch → PickStreamDownloadKey → DownloadPrepare → StreamChunked/FFmpeg/Direct
- **GET /stream/m4a** → ResolveFormats → `bestaudio[ext=m4a]/bestaudio` → StreamDirect (no ffmpeg)
- **GET /stream/mp3** → Fetch → DownloadPrepare → StreamFFmpeg (libmp3lame 192k)

### Twitter/X (`/fetch`, `/download`)
- Sama dengan generic path, tapi session `platform` = "twitter" atau "x"
- Python worker menangani direct proxy (bypass CDN URL extraction)

---

## Konfigurasi

Semua via environment variables:

| Variabel | Default | Deskripsi |
|----------|---------|-----------|
| `GATEWAY_PORT` | `9111` | Port HTTP server |
| `WORKER_COUNT` | `3` | Jumlah Python worker |
| `EXTRACTOR_IPC_ENABLED` | `false` | Enable IPC ke Python worker |
| `EXTRACTOR_TIMEOUT_MS` | `45000` | Timeout per IPC call |
| `GLUETUN_PASSWORD` | `secretpassword` | Gluetun control server password |
| `MAX_RETRIES` | `3` | Retry attempts per request |
| `HEALTH_CHECK_TIMEOUT_MS` | `8000` | TCP health check timeout |
| `HEALTH_MONITOR_INTERVAL_MS` | `5000` | Health probe interval |
| `HEALTH_FAILURE_THRESHOLD` | `3` | Consecutive failures sebelum unhealthy |
| `RATE_LIMIT_COOLDOWN` | `300` | Seconds cooldown setelah rate limit |
| `RESTART_BACKOFF_BASE` | `30` | Base delay untuk restart backoff |
| `RESTART_BACKOFF_MAX` | `300` | Max delay untuk restart backoff |
| `RESTART_BUDGET_LIMIT` | `3` | Max restart dalam window sebelum quarantine |
| `RESTART_BUDGET_WINDOW` | `600` | Window untuk restart budget (detik) |
| `RESTART_QUARANTINE_SECONDS` | `600` | Quarantine duration setelah budget habis |
| `RESTART_BACKOFF_JITTER` | `5` | Random jitter ke backoff |
| `DEGRADED_RETRY_AFTER` | `5` | Retry-After header value |
| `GATEWAY_RL_WINDOW_SECONDS` | `60` | Rate limit window |
| `GATEWAY_RL_FETCH_LIMIT` | `45` | Max fetch requests per window |
| `GATEWAY_RL_DOWNLOAD_LIMIT` | `45` | Max download requests per window |
| `DRAIN_TIMEOUT_SECONDS` | `90` | Max waktu tunggu drain saat restart |
| `DRAIN_POLL_INTERVAL_MS` | `500` | Poll interval saat drain |
| `RESTART_STABILIZE_SECONDS` | `2` | Pause setelah restart berhasil |
| `UNHEALTHY_RESTART_THRESHOLD` | `6` | Consecutive probe failures untuk auto-restart |

---

## Migration Checklist

### Yang sudah dipindahkan
- [x] `SlidingWindowRateLimiter` → `utils/rate_limiter.go`
- [x] `Config` + `loadConfig()` → `utils/config.go`
- [x] `Worker`, `WorkerRegistry` → `registry/worker.go`
- [x] `VPNRotator` → `registry/rotator.go`
- [x] `DeliveryPlan`, `ParseDeliveryPlan`, `ResolveDeliveryMode` → `delivery/plan.go`
- [x] `mediaHTTPClient`, `mediaHTTPClientForPlan`, `isClientAbortError`, `CopyHeader` → `delivery/delivery.go`
- [x] `streamDirectPlanWithRefresh` → `delivery/direct.go` (+ TikTok 403 body check)
- [x] `streamChunkedRangeWithRefresh`, `downloadPlanSourceToFile`, `downloadPlanSourceToWriter` → `delivery/chunked.go`
- [x] `streamWithFFmpegFromPlan` → `delivery/ffmpeg.go`
- [x] `renderTikTokSlideshowFromPlan`, `streamTikTokSlideshowFromPlan`, `downloadToFile` → `delivery/tiktok_slideshow.go`
- [x] `proxyWithRetry`, `proxyWithRotation`, `proxyStreamingResponse`, `handleResponse`, `proxyToWorker`, `streamFromWorker` → `handlers/router.go`
- [x] Semua `handle*` methods → `handlers/handlers.go`
- [x] `ServeHTTP`, restart scheduler, uptime checker, health monitor → `handlers/router.go`
- [x] Bootstrap + adapters → `main.go`

### Yang tidak berubah
- IPC protocol (TCP JSON-RPC newline-delimited, port 9487)
- Python worker daemon (`extractor/worker_daemon.py`)
- Python worker server (`extractor/worker_server.py`)
- Docker Compose setup (`docker-compose.warp.2.yml`)
- Semua route paths, method signatures, response contract
- Video chunk size (10MB), Audio chunk size (8MB)
- FFmpeg arguments semua mode
- Worker state machine (healthy/unhealthy/quarantine/backoff)
- VPN rotation flow (drain → restart gluetun → restart ytdlp → health check)
- Rate limiting (sliding window per IP)
- Response body pattern detection (geo-restrict, rate-limit, captcha, IP-block)

---

## Testing

### Build
```bash
cd yt-dlp-stream/gateway-go
go build ./...      # harus clean, tanpa error
go vet ./...        # harus clean, tanpa warning
go build -o gateway-go .
```

### Manual Testing (lokal, tanpa Docker)
```bash
# Test root endpoint
curl http://localhost:9111/

# Test health
curl http://localhost:9111/health

# Test TikTok submission (perlu Python worker running)
curl -X POST http://localhost:9111/tiktok \
  -d '{"url":"https://www.tiktok.com/@user/video/123"}'

# Test generic fetch
curl "http://localhost:9111/fetch?url=https://www.youtube.com/watch?v=..."
```

### Docker Testing (dengan docker-compose.warp.2.yml)
```bash
# Build dan start
cd yt-dlp-stream
docker compose -f docker-compose.warp.2.yml build gateway-warp
docker compose -f docker-compose.warp.2.yml up -d

# Test
curl http://localhost:9111/
curl http://localhost:9111/health
```

---

## Catatan Penting

### 1. TikTok 403 Body Check (fix dari original)
Di original `main.go`, TikTok download cek body response 403 sebelum refresh:
```go
if resp.StatusCode == http.StatusForbidden {
    body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
    if !shouldRefreshTikTokForbidden(body) {
        // permanent: geo-block, captcha, login → JANGAN refresh
        w.WriteHeader(403)
        w.Write(body)
        return
    }
    // transient → refresh URL
}
```
Di versi refactor ini, `ShouldRefreshTikTokForbidden()` dipanggil di `delivery/direct.go:StreamDirect()` dengan **behavior identik**.

### 2. Platform Detection
Platform (`tiktok`, `douyin`, `youtube`, `twitter`) datang dari Python worker via IPC response field `platform`. Go menggunakan ini untuk:
- **Bypass worker proxy** untuk media TikTok (`tiktokcdn.com`, `muscdn.com`, `byteoversea.com`)
- **Enable 403 body check** sebelum refresh URL
- **Routing slideshow** ke ffmpeg concat

### 3. Mengapa `extractor/` tidak di-refactor?
Folder `extractor/` berisi Python worker daemon yang berjalan di container terpisah (di dalam Gluetun network). Worker ini yang ngobrol dengan Go via TCP socket. Refactoring Python worker adalah scope terpisah — IPC interface (TCP JSON-RPC) sudah stabil dan tidak perlu diubah.
