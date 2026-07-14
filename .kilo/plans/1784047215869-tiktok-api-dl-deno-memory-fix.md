# Plan: Fix memory growth in `tiktok-api-dl` + migrate Bun → Deno

## Goal

Stop the multi-day RSS climb (~20MB → ~95MB / 5 days) on the TikTok API container by:

1. Fixing **app-level** leaks that exist on any runtime
2. Migrating runtime **Bun → Deno** (user preference; Bun often grows on long-lived servers)
3. Adding Docker memory guardrails so a bad process restarts instead of eating the host

## Findings (code review)

Graph is classic unbounded growth after a restart dip on 07-09. Not Redis (capped at 128MB LRU). Main process has no `mem_limit`.

| Priority | Issue | Where | Why it grows |
|---|---|---|---|
| P0 | Slideshow fully buffers MP4 into RAM | `src/index.ts` `streamSlideshow` + `readSlideshowFile` | Every slideshow download holds full file as `Buffer` |
| P0 | Temp dir bug (mkdir wrong path) | `src/slideshow.ts` L54–60 | Creates dir A, uses dir B → orphaned `/tmp` + failed cleanup |
| P0 | `cleanupTemp` not awaited; skipped on error | `streamSlideshow`, `renderSlideshow` | Temp files / partial ffmpeg output accumulate |
| P1 | ffmpeg timeout race | `renderSlideshow` | `setTimeout` may not clear; stderr may hang if never consumed on success |
| P1 | `deleteSession` imported, never used | `index.ts` / download path | Sessions live full TTL (5m) even after one-shot download (Redis mitigates; still noise) |
| P2 | No container memory limit | `docker-compose.yml` | Process can climb indefinitely until host pressure |
| P2 | Bun long-running RSS growth | `Dockerfile` `oven/bun` | Runtime-level; Deno avoids this class of Bun issues |

**Not the main leak (prod uses Redis):** in-memory session/cache Maps only apply when `REDIS_URL` is empty.

**Hono note:** Hono is a router, not a runtime. Deno is the correct swap if the goal is leaving Bun.

## Decisions

| Decision | Choice |
|---|---|
| Runtime | **Deno** (Docker image `denoland/deno`), Node compat for `node:*` + npm deps |
| Framework | Keep thin `node:http` server (or Deno `Deno.serve` if Node HTTP proves awkward). **No Hono** unless needed later |
| App leak fixes | Required in same change set (Deno alone will not fix slideshow buffer) |
| Docker | `mem_limit` + `memswap_limit` + restart on OOM for `tiktok-api-prod` |
| Cache/session | Keep Redis as today (`REDIS_URL=redis://redis:6379/0`) |
| Scope out | Heap profiling dashboards, rewriting `@tobyg74/tiktok-api-dl` |

## Target architecture

```
Client → Deno process (node:http or Deno.serve)
           ├─ Redis sessions (TTL 300s)
           ├─ Redis extract cache (TTL 1800s)
           ├─ GET /tiktok/download → stream CDN (no full buffer)
           └─ slideshow → ffmpeg via Deno.Command → stream file → always cleanupTemp
```

## Implementation tasks

### 1. Fix slideshow memory + temp lifecycle (`src/slideshow.ts`, `src/index.ts`)

- Fix temp dir creation: single `tempDir` path, `mkdir` that path once (remove dual `Date.now()` bug).
- On **all** exit paths (success, ffmpeg fail, timeout, download fail): `await cleanupTemp(tempDir)` in `finally`.
- Clear ffmpeg timeout handle when process exits.
- Drain or close `stdout`/`stderr` pipes so the child cannot block.
- **Stop full-buffer response:**
  - Prefer stream file to client (`createReadStream` / Deno open + pipe) with `Content-Length` from `stat`.
  - Or: stream ffmpeg stdout directly to `res` (harder with filter_complex file output; file + stream is fine).
- Remove `readSlideshowFile` full-file `Buffer` path from hot path (or keep only for tiny tests).

### 2. Download path hygiene (`src/index.ts`)

- After successful download start (or on complete), optionally `deleteSession(key)` for one-shot links (document: first download invalidates key).
- Ensure `AbortController` + stream destroy still work under Deno.
- Cap `readBody` size (e.g. 64KB) so oversized POSTs cannot allocate unbounded memory.

### 3. Migrate Bun → Deno

**Files**

| File | Change |
|---|---|
| `Dockerfile` | Base `denoland/deno:2.x` (or current stable). Install ffmpeg. `CMD deno run --allow-net --allow-env --allow-read --allow-write --allow-run --allow-sys --node-modules-dir=auto src/index.ts` (tune flags as needed) |
| `package.json` | Keep for npm deps; add `deno.json` with tasks, imports if useful |
| `src/slideshow.ts` | Replace `Bun.spawn` with `Deno.Command` |
| `docker-compose.yml` | Point prod/dev at Deno image/command; remove Bun-specific watch if needed |
| `README.md` | Document Deno run + permissions |

**Deps**

- Keep `ioredis` and `@tobyg74/tiktok-api-dl` via npm/`node_modules` (Deno nodeCompat).
- Verify axios/undici inside `@tobyg74/tiktok-api-dl` work under Deno; if not, pin Node compat flags (`--unstable-node-globals` / Deno 2 nodeCompat as required).

**Dev profile**

- Replace `bun --watch` with `deno run --watch` (or drop watch in Docker and run watch only on host).

### 4. Docker hardening (`docker-compose.yml`)

On `tiktok-api-prod` (and optionally dev):

```yaml
mem_limit: 384m          # or 512m if slideshows need headroom
memswap_limit: 384m
restart: unless-stopped
# optional: pids_limit for runaway ffmpeg
```

Keep Redis `maxmemory 128mb allkeys-lru` as-is.

### 5. Observability (lightweight)

Extend `/health` with process RSS (Deno/Node memory API) so growth is visible without external graphs:

```json
{ "rss_mb": 42.1, "heap_used_mb": 18.3, "active_sessions": N }
```

## Failure modes

| Risk | Mitigation |
|---|---|
| `@tobyg74/tiktok-api-dl` breaks under Deno | Smoke-test extract v1/v2/v3 before cutover; fallback keep Node binary if Deno fails (out of scope unless blocked) |
| Slideshow stream + early client disconnect leaves file | `finally` cleanup + `res.on("close")` |
| Concurrent slideshow OOMs under 384MB | Sequential downloads already; lower ffmpeg threads; reject if many concurrent (optional semaphore, out of scope unless needed) |
| Permission denials on Deno | Explicit `--allow-*` in Dockerfile CMD; document in README |

## Validation

1. **Unit-ish / manual**
   - Photo post extract → slideshow download → confirm temp dir gone under `/tmp` after request
   - Video download stream completes; RSS does not jump by full video size
2. **Load**
   - N parallel `/tiktok` + `/tiktok/download` for ~10–20 min; RSS should plateau, not climb linearly
3. **Docker**
   - `docker stats` after deploy: RSS under limit; OOM restart if forced over limit
4. **Regression**
   - `/health` shows Redis backends
   - Existing API contract unchanged (`/tiktok`, `/tiktok/download` shapes)

## Rollout

1. Implement app fixes + Deno Dockerfile on branch
2. Build image, run compose `--profile prod` against staging/local
3. Smoke extract + download video + slideshow
4. Deploy; watch memory 24–48h (should flatten vs previous 5-day climb)
5. If Deno blocks on dependency, ship **app fixes on Bun first** as hotfix, finish Deno after

## Out of scope

- Migrating other services in the monorepo to Deno
- Rewriting the TikTok downloader library
- Full APM / continuous heap snapshots
- Hono rewrite

## File touch list

- `tiktok-api-dl/src/slideshow.ts` — temp dir, Deno.Command, cleanup, no hang pipes
- `tiktok-api-dl/src/index.ts` — stream slideshow, body size cap, optional session delete, health RSS
- `tiktok-api-dl/Dockerfile` — Deno base + permissions CMD
- `tiktok-api-dl/docker-compose.yml` — mem limits, Deno command/env
- `tiktok-api-dl/deno.json` — new (tasks/config)
- `tiktok-api-dl/package.json` / README — scripts + run docs
- `tiktok-api-dl/src/session.ts` / `extraction_cache.ts` — only if Deno Redis client needs tweaks (prefer keep as-is)
