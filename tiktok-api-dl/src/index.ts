import * as http from "node:http";
import { Buffer } from "node:buffer";
import { createReadStream } from "node:fs";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import {
  extractPost,
  isTiktokUrl,
  sanitizeFilenamePart,
  type DownloaderVersion,
} from "./tiktok.ts";
import {
  claimSession,
  restoreSession,
  activeSessionCount,
  closeSessionStore,
  isRedisBackend,
  type SessionData,
} from "./session.ts";
import { closeExtractionCache, isExtractCacheRedis, extractCacheTtl } from "./extraction_cache.ts";
import {
  renderSlideshow,
  cleanupTemp,
  slideshowFileSize,
  SlideshowError,
  activeSlideshowCount,
} from "./slideshow.ts";

const PORT = Number(process.env.PORT) || 7788;
const DEFAULT_VERSION = (process.env.DEFAULT_VERSION || "v1") as DownloaderVersion;
const TIKTOK_API_KEY = process.env.TIKTOK_API_KEY || "";
const FFMPEG_PATH = process.env.FFMPEG_PATH || "ffmpeg";
const MAX_BODY_BYTES = Number(process.env.MAX_BODY_BYTES) || 64 * 1024;

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Accept, X-API-Key",
};

const CDN_HEADERS: Record<string, string> = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  Referer: "https://www.tiktok.com/",
};

function runtimeName(): "deno" | "node" {
  return typeof (globalThis as any).Deno !== "undefined" ? "deno" : "node";
}

function sendJson(res: ServerResponse, body: unknown, status = 200, extraHeaders: Record<string, string> = {}) {
  const payload = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": String(payload.byteLength),
    ...CORS_HEADERS,
    ...extraHeaders,
  });
  res.end(payload);
}

function sendErrorJson(res: ServerResponse, message: string, status = 500, code?: string) {
  const body: Record<string, string> = { error: message };
  if (code) body.code = code;
  sendJson(res, body, status);
}

function parseQuery(req: IncomingMessage): URLSearchParams {
  return new URL(req.url || "/", "http://localhost").searchParams;
}

async function readBody(req: IncomingMessage): Promise<any> {
  try {
    const chunks: Buffer[] = [];
    let total = 0;
    for await (const chunk of req) {
      const buf = chunk as Buffer;
      total += buf.byteLength;
      if (total > MAX_BODY_BYTES) {
        req.destroy();
        throw new Error(`Request body exceeds ${MAX_BODY_BYTES} bytes`);
      }
      chunks.push(buf);
    }
    const raw = Buffer.concat(chunks).toString("utf8");
    if (!raw) return {};
    return JSON.parse(raw);
  } catch (err: any) {
    if (err?.message?.includes("exceeds")) throw err;
    return {};
  }
}

function getVersion(value: string | null | undefined): DownloaderVersion {
  if (value === "v2" || value === "v3") return value;
  return DEFAULT_VERSION;
}

function parseBool(value: string | null | undefined, fallback: boolean): boolean {
  if (value === null || value === undefined || value === "") return fallback;
  const v = value.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(v)) return true;
  if (["0", "false", "no", "off"].includes(v)) return false;
  return fallback;
}

function authorize(req: IncomingMessage): boolean {
  if (!TIKTOK_API_KEY) return true;
  let key = req.headers["x-api-key"];
  if (Array.isArray(key)) key = key[0];
  if (!key) key = parseQuery(req).get("api_key") || undefined;
  return key === TIKTOK_API_KEY;
}

function buildContentDisposition(filename: string, download: boolean): string {
  const disposition = download ? "attachment" : "inline";
  const ascii = filename.replace(/[^\x20-\x7E]/g, "");
  const safe = ascii.replace(/"/g, "'");
  const utf8 = encodeURIComponent(filename);
  if (ascii === filename) {
    return `${disposition}; filename="${safe}"`;
  }
  return `${disposition}; filename="${safe}"; filename*=UTF-8''${utf8}`;
}

function memoryStats(): { rss_mb: number; heap_used_mb: number | null } {
  const mu = process.memoryUsage();
  return {
    rss_mb: Math.round((mu.rss / (1024 * 1024)) * 10) / 10,
    heap_used_mb: Math.round((mu.heapUsed / (1024 * 1024)) * 10) / 10,
  };
}

function clientAbortController(res: ServerResponse): AbortController {
  const controller = new AbortController();
  const onClose = () => {
    if (!res.writableEnded) controller.abort();
  };
  res.on("close", onClose);
  return controller;
}

// ---------- Handlers ----------

function handleRoot(res: ServerResponse): void {
  sendJson(res, {
    service: "tiktok-api-dl-server",
    status: "ok",
    transport: "deno + tiktok-api-dl",
    endpoints: ["/", "/health", "/tiktok", "/tiktok/download"],
  });
}

async function handleHealth(res: ServerResponse): Promise<void> {
  const sessions = await activeSessionCount();
  const mem = memoryStats();
  sendJson(res, {
    status: "ok",
    time: new Date().toISOString(),
    version: DEFAULT_VERSION,
    runtime: runtimeName(),
    active_sessions: sessions,
    active_slideshows: activeSlideshowCount(),
    session_backend: isRedisBackend() ? "redis" : "memory",
    extract_cache_backend: isExtractCacheRedis() ? "redis" : "memory",
    extract_cache_ttl_seconds: extractCacheTtl(),
    ffmpeg: FFMPEG_PATH,
    ...mem,
  });
}

async function handleTikTok(req: IncomingMessage, res: ServerResponse): Promise<void> {
  if (req.method !== "POST") {
    return sendErrorJson(res, "Method not allowed", 405);
  }
  if (!authorize(req)) {
    return sendErrorJson(res, "Invalid or missing API Key", 401, "unauthorized");
  }

  let body: any;
  try {
    body = await readBody(req);
  } catch (err: any) {
    return sendErrorJson(res, err?.message || "Invalid body", 413, "payload_too_large");
  }

  const url = (body?.url || "").trim();
  if (!url) return sendErrorJson(res, "URL is required", 400, "bad_request");
  if (!isTiktokUrl(url)) return sendErrorJson(res, "Only TikTok/Douyin URLs are supported", 400, "bad_request");

  const version = getVersion(body?.version);
  const proxy = body?.proxy ? String(body.proxy).trim() : undefined;
  const impersonate = body?.impersonate ? String(body.impersonate).trim() : undefined;

  try {
    const result = await extractPost(url, { version, proxy, impersonate });
    sendJson(res, result);
  } catch (err: any) {
    const msg = err?.message || "Failed to extract TikTok post";
    if (/Unsupported URL|not found|Unable to|Could not extract/i.test(msg)) {
      return sendErrorJson(res, msg, 400, "not_found");
    }
    if (/blocked|private|restricted|403|captcha|verify/i.test(msg)) {
      return sendErrorJson(res, msg, 403, "ip_blocked");
    }
    sendErrorJson(res, msg, 500, "internal_error");
  }
}

async function handleTikTokDownload(req: IncomingMessage, res: ServerResponse): Promise<void> {
  if (req.method !== "GET") {
    return sendErrorJson(res, "Method not allowed", 405);
  }

  const q = parseQuery(req);
  const key = (q.get("key") || "").trim();
  if (!key) return sendErrorJson(res, "Missing key query parameter", 400, "bad_request");

  const download = parseBool(q.get("download"), true);

  // Atomic claim: only one concurrent consumer gets the session.
  const session = await claimSession(key);
  if (!session) {
    return sendErrorJson(res, "Download link expired or invalid", 404, "not_found");
  }

  const author = sanitizeFilenamePart(session.author, "tiktok");
  let delivered = false;

  try {
    if (session.type === "slideshow") {
      delivered = await streamSlideshow(res, session, author, download);
    } else if (session.type === "video") {
      const quality = session.quality || "video";
      const filename = `${author}_${quality}.mp4`;
      delivered = await streamDirect(res, session, filename, "video/mp4", download);
    } else if (session.type === "photo") {
      const idx = session.photo_index || 1;
      const filename = `${author}_photo_${idx}.jpg`;
      delivered = await streamDirect(res, session, filename, "image/jpeg", download);
    } else if (session.type === "mp3") {
      const filename = `${author}.mp3`;
      delivered = await streamDirect(res, session, filename, "audio/mpeg", download);
    } else {
      sendErrorJson(res, `Unknown content type: ${session.type}`, 400, "bad_request");
    }
  } catch (err: any) {
    if (res.headersSent) {
      res.destroy(err instanceof Error ? err : new Error(String(err)));
      return;
    }
    if (err instanceof SlideshowError) {
      return sendErrorJson(res, err.message, err.status, "slideshow_error");
    }
    const msg = err?.message || "Failed to stream media";
    sendErrorJson(res, msg, 502, "upstream_error");
  } finally {
    // Restore claimed session if media was never successfully started/finished.
    if (!delivered && !res.writableEnded) {
      await restoreSession(session).catch(() => {});
    }
  }
}

async function streamDirect(
  res: ServerResponse,
  session: SessionData,
  filename: string,
  contentType: string,
  download: boolean
): Promise<boolean> {
  const directUrl = session.direct_url;
  if (!directUrl) {
    sendErrorJson(res, "No media URL available in session", 400, "bad_request");
    return false;
  }

  const headers: Record<string, string> = { ...CDN_HEADERS, ...(session.http_headers || {}) };
  if (session.cookies) headers["Cookie"] = session.cookies;

  const controller = clientAbortController(res);

  let upstream: Response;
  try {
    upstream = await fetch(directUrl, {
      headers,
      redirect: "follow",
      signal: controller.signal,
    });
  } catch (err: any) {
    if (controller.signal.aborted) return false;
    sendErrorJson(res, err?.message || "Failed to reach upstream CDN", 502, "upstream_error");
    return false;
  }

  if (!upstream.ok) {
    sendErrorJson(res, `Upstream CDN returned ${upstream.status}`, 502, "upstream_error");
    return false;
  }
  if (!upstream.body) {
    sendErrorJson(res, "Upstream CDN returned an empty body", 502, "upstream_error");
    return false;
  }

  const respHeaders: Record<string, string> = {
    "Content-Type": contentType,
    "Content-Disposition": buildContentDisposition(filename, download),
    ...CORS_HEADERS,
  };
  const upstreamLength = upstream.headers.get("content-length");
  if (upstreamLength) respHeaders["Content-Length"] = upstreamLength;

  res.writeHead(200, respHeaders);

  const nodeStream = Readable.fromWeb(upstream.body as any);
  try {
    await pipeline(nodeStream, res);
    return true;
  } catch (err: any) {
    if (controller.signal.aborted || res.destroyed) return true;
    console.error("Upstream stream error while piping:", err);
    if (!res.destroyed) res.destroy(err instanceof Error ? err : new Error(String(err)));
    return true;
  }
}

async function streamSlideshow(
  res: ServerResponse,
  session: SessionData,
  author: string,
  download: boolean
): Promise<boolean> {
  const photoUrls = session.photo_urls || [];
  const audioUrl = session.audio_url;
  if (photoUrls.length === 0) {
    sendErrorJson(res, "No photos available for slideshow", 400, "bad_request");
    return false;
  }

  const controller = clientAbortController(res);

  let outputPath: string | undefined;
  let tempDir: string | undefined;
  try {
    const rendered = await renderSlideshow(photoUrls, audioUrl, {
      ffmpegPath: FFMPEG_PATH,
      signal: controller.signal,
    });
    outputPath = rendered.outputPath;
    tempDir = rendered.tempDir;

    if (controller.signal.aborted || res.writableEnded) {
      return false;
    }

    const size = await slideshowFileSize(outputPath);
    const filename = `${author}_slideshow.mp4`;
    res.writeHead(200, {
      "Content-Type": "video/mp4",
      "Content-Disposition": buildContentDisposition(filename, download),
      "Content-Length": String(size),
      "X-Accel-Buffering": "no",
      ...CORS_HEADERS,
    });

    const fileStream = createReadStream(outputPath);
    try {
      await pipeline(fileStream, res);
      return true;
    } catch {
      // Client disconnect or stream error after headers: treat as consumed.
      return true;
    }
  } catch (err: any) {
    if (err instanceof SlideshowError && err.status === 499) {
      return false;
    }
    throw err;
  } finally {
    if (tempDir) await cleanupTemp(tempDir);
  }
}

// ---------- Server ----------

const server = http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url || "/", "http://localhost");
  const method = req.method || "GET";

  if (method === "OPTIONS") {
    res.writeHead(204, CORS_HEADERS);
    res.end();
    return;
  }

  try {
    if (pathname === "/") return handleRoot(res);
    if (pathname === "/health") return await handleHealth(res);
    if (pathname === "/tiktok") return await handleTikTok(req, res);
    if (pathname === "/tiktok/download") return await handleTikTokDownload(req, res);
    return sendErrorJson(res, "Route not found", 404, "not_found");
  } catch (err: any) {
    console.error("Unhandled error:", err);
    if (!res.headersSent) {
      sendErrorJson(res, err?.message || "Unexpected server error", 500, "internal_error");
    } else {
      res.destroy(err instanceof Error ? err : new Error(String(err)));
    }
  }
});

server.listen(PORT, () => {
  console.log(`tiktok-api-dl-server listening on http://localhost:${PORT} (${runtimeName()})`);
  console.log(`Default downloader version: ${DEFAULT_VERSION}`);
  console.log(`FFmpeg path: ${FFMPEG_PATH}`);
  if (process.env.REDIS_URL) {
    console.log(`Redis URL: ${process.env.REDIS_URL.replace(/:[^@]+@/, ":***@")}`);
  } else {
    console.log("Redis: not configured (using in-memory session store)");
  }
  console.log(`Extraction cache: TTL ${extractCacheTtl()}s, backend ${isExtractCacheRedis() ? "redis" : "memory"}`);
  console.log(`Endpoints:`);
  console.log(`  GET  /`);
  console.log(`  GET  /health`);
  console.log(`  POST /tiktok            (body: {url, version?, proxy?, impersonate?})`);
  console.log(`  GET  /tiktok/download   (query: key, download?)`);
});

async function shutdown() {
  await closeSessionStore();
  await closeExtractionCache();
  server.close(() => process.exit(0));
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
