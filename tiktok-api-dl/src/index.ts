import {
  extractPost,
  isTiktokUrl,
  sanitizeFilenamePart,
  type DownloaderVersion,
} from "./tiktok.ts";
import { getSession, deleteSession, activeSessionCount, closeSessionStore, isRedisBackend, type SessionData } from "./session.ts";
import { closeExtractionCache, isExtractCacheRedis, extractCacheTtl } from "./extraction_cache.ts";
import { renderSlideshow, cleanupTemp, readSlideshowFile, SlideshowError } from "./slideshow.ts";

const PORT = Number(process.env.PORT) || 7788;
const DEFAULT_VERSION = (process.env.DEFAULT_VERSION || "v1") as DownloaderVersion;
const TIKTOK_API_KEY = process.env.TIKTOK_API_KEY || "";
const FFMPEG_PATH = process.env.FFMPEG_PATH || "ffmpeg";

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

function json(body: unknown, status = 200, extraHeaders: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS, ...extraHeaders },
  });
}

function errorJson(message: string, status = 500, code?: string) {
  const body: Record<string, string> = { error: message };
  if (code) body.code = code;
  return json(body, status);
}

function parseQuery(request: Request): URLSearchParams {
  return new URL(request.url).searchParams;
}

async function readBody(request: Request): Promise<any> {
  try {
    return await request.json();
  } catch {
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

function authorize(request: Request): boolean {
  if (!TIKTOK_API_KEY) return true;
  let key = request.headers.get("X-API-Key");
  if (!key) {
    key = parseQuery(request).get("api_key");
  }
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

// ---------- Handlers ----------

function handleRoot(): Response {
  return json({
    service: "tiktok-api-dl-server",
    status: "ok",
    transport: "bun + tiktok-api-dl",
    endpoints: ["/", "/health", "/tiktok", "/tiktok/download"],
  });
}

async function handleHealth(): Promise<Response> {
  const sessions = await activeSessionCount();
  return json({
    status: "ok",
    time: new Date().toISOString(),
    version: DEFAULT_VERSION,
    active_sessions: sessions,
    session_backend: isRedisBackend() ? "redis" : "memory",
    extract_cache_backend: isExtractCacheRedis() ? "redis" : "memory",
    extract_cache_ttl_seconds: extractCacheTtl(),
    ffmpeg: FFMPEG_PATH,
  });
}

async function handleTikTok(request: Request): Promise<Response> {
  if (request.method !== "POST") {
    return errorJson("Method not allowed", 405);
  }
  if (!authorize(request)) {
    return errorJson("Invalid or missing API Key", 401, "unauthorized");
  }

  const body = await readBody(request);
  const url = (body?.url || "").trim();
  if (!url) return errorJson("URL is required", 400, "bad_request");
  if (!isTiktokUrl(url)) return errorJson("Only TikTok/Douyin URLs are supported", 400, "bad_request");

  const version = getVersion(body?.version);
  const proxy = body?.proxy ? String(body.proxy).trim() : undefined;
  const impersonate = body?.impersonate ? String(body.impersonate).trim() : undefined;

  try {
    const result = await extractPost(url, { version, proxy, impersonate });
    return json(result);
  } catch (err: any) {
    const msg = err?.message || "Failed to extract TikTok post";
    if (/Unsupported URL|not found|Unable to|Could not extract/i.test(msg)) {
      return errorJson(msg, 400, "not_found");
    }
    if (/blocked|private|restricted|403|captcha|verify/i.test(msg)) {
      return errorJson(msg, 403, "ip_blocked");
    }
    return errorJson(msg, 500, "internal_error");
  }
}

async function handleTikTokDownload(request: Request): Promise<Response> {
  if (request.method !== "GET") {
    return errorJson("Method not allowed", 405);
  }

  const q = parseQuery(request);
  const key = (q.get("key") || "").trim();
  if (!key) return errorJson("Missing key query parameter", 400, "bad_request");

  const download = parseBool(q.get("download"), true);

  const session = await getSession(key);
  if (!session) {
    return errorJson("Download link expired or invalid", 404, "not_found");
  }

  const author = sanitizeFilenamePart(session.author, "tiktok");

  try {
    if (session.type === "slideshow") {
      return await streamSlideshow(session, author, download);
    }
    if (session.type === "video") {
      const quality = session.quality || "video";
      const filename = `${author}_${quality}.mp4`;
      return await streamDirect(session, filename, "video/mp4", download);
    }
    if (session.type === "photo") {
      const idx = session.photo_index || 1;
      const filename = `${author}_photo_${idx}.jpg`;
      return await streamDirect(session, filename, "image/jpeg", download);
    }
    if (session.type === "mp3") {
      const filename = `${author}.mp3`;
      return await streamDirect(session, filename, "audio/mpeg", download);
    }
    return errorJson(`Unknown content type: ${session.type}`, 400, "bad_request");
  } catch (err: any) {
    if (err instanceof SlideshowError) {
      return errorJson(err.message, err.status, "slideshow_error");
    }
    const msg = err?.message || "Failed to stream media";
    return errorJson(msg, 502, "upstream_error");
  }
}

async function streamDirect(
  session: SessionData,
  filename: string,
  contentType: string,
  download: boolean
): Promise<Response> {
  const directUrl = session.direct_url;
  if (!directUrl) return errorJson("No media URL available in session", 400, "bad_request");

  const headers: Record<string, string> = { ...CDN_HEADERS, ...(session.http_headers || {}) };
  if (session.cookies) headers["Cookie"] = session.cookies;

  const upstream = await fetch(directUrl, {
    headers,
    redirect: "follow",
  });

  if (!upstream.ok && upstream.status !== 206) {
    return errorJson(`Upstream CDN returned ${upstream.status}`, 502, "upstream_error");
  }

  const respHeaders = new Headers();
  respHeaders.set("Content-Type", contentType);
  respHeaders.set("Content-Disposition", buildContentDisposition(filename, download));
  respHeaders.set("X-Accel-Buffering", "no");
  const cl = upstream.headers.get("content-length");
  if (cl) respHeaders.set("Content-Length", cl);
  const cr = upstream.headers.get("content-range");
  if (cr) respHeaders.set("Content-Range", cr);
  for (const [k, v] of Object.entries(CORS_HEADERS)) respHeaders.set(k, v);

  return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

async function streamSlideshow(
  session: SessionData,
  author: string,
  download: boolean
): Promise<Response> {
  const photoUrls = session.photo_urls || [];
  const audioUrl = session.audio_url;
  if (photoUrls.length === 0) {
    return errorJson("No photos available for slideshow", 400, "bad_request");
  }

  const { outputPath, tempDir } = await renderSlideshow(photoUrls, audioUrl, {
    ffmpegPath: FFMPEG_PATH,
  });

  const buf = await readSlideshowFile(outputPath);
  // cleanup async after reading
  cleanupTemp(tempDir);

  const filename = `${author}_slideshow.mp4`;
  const headers = new Headers({
    "Content-Type": "video/mp4",
    "Content-Disposition": buildContentDisposition(filename, download),
    "Content-Length": String(buf.byteLength),
    "X-Accel-Buffering": "no",
    ...CORS_HEADERS,
  });

  return new Response(buf, { status: 200, headers });
}

// ---------- Server ----------

const server = Bun.serve({
  port: PORT,
  async fetch(request) {
    const { pathname } = new URL(request.url);
    const method = request.method;

    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    try {
      if (pathname === "/") return handleRoot();
      if (pathname === "/health") return await handleHealth();
      if (pathname === "/tiktok") return await handleTikTok(request);
      if (pathname === "/tiktok/download") return await handleTikTokDownload(request);
      return errorJson("Route not found", 404, "not_found");
    } catch (err: any) {
      console.error("Unhandled error:", err);
      return errorJson(err?.message || "Unexpected server error", 500, "internal_error");
    }
  },
});

console.log(`tiktok-api-dl-server listening on http://localhost:${server.port}`);
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

process.on("SIGINT", async () => {
  await closeSessionStore();
  await closeExtractionCache();
  server.stop();
  process.exit(0);
});
process.on("SIGTERM", async () => {
  await closeSessionStore();
  await closeExtractionCache();
  server.stop();
  process.exit(0);
});
