import {
  fetchPost,
  resolveDownload,
  isTiktokUrl,
  type DownloaderVersion,
  type MediaType,
} from "./tiktok.ts";

const PORT = Number(process.env.PORT) || 7788;
const DEFAULT_VERSION = (process.env.DEFAULT_VERSION || "v1") as DownloaderVersion;

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function json(body: unknown, status = 200, extraHeaders: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS, ...extraHeaders },
  });
}

function errorJson(message: string, status = 500) {
  return json({ status: "error", error: message }, status);
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

function getMediaType(value: string | null): MediaType | null {
  if (!value) return null;
  const v = value.toLowerCase();
  if (v === "video" || v === "image" || v === "music") return v;
  return null;
}

function getVersion(value: string | null): DownloaderVersion {
  if (value === "v2" || value === "v3") return value;
  return DEFAULT_VERSION;
}

async function handleHealth(): Promise<Response> {
  return json({
    status: "ok",
    service: "tiktok-api-dl-server",
    time: new Date().toISOString(),
    version: DEFAULT_VERSION,
    endpoints: ["/health", "/fetch", "/download"],
  });
}

async function handleFetch(request: Request): Promise<Response> {
  let url: string | null = null;
  let version: DownloaderVersion = DEFAULT_VERSION;

  if (request.method === "GET") {
    const q = parseQuery(request);
    url = q.get("url");
    version = getVersion(q.get("version"));
  } else if (request.method === "POST") {
    const body = await readBody(request);
    url = body?.url || null;
    version = getVersion(body?.version || null);
  }

  if (!url) return errorJson("Parameter 'url' is required", 400);
  if (!isTiktokUrl(url)) return errorJson("Only TikTok/Douyin URLs are supported", 400);

  try {
    const result = await fetchPost(url, { version });
    return json(result);
  } catch (err: any) {
    const msg = err?.message || "Failed to fetch TikTok post";
    if (/Unsupported URL|not found|Unable to/i.test(msg)) return errorJson(msg, 404);
    if (/blocked|private|restricted|403/i.test(msg)) return errorJson(msg, 403);
    return errorJson(msg, 500);
  }
}

async function handleDownload(request: Request): Promise<Response> {
  const q = parseQuery(request);
  const url = q.get("url");
  const type = getMediaType(q.get("type")) || "video";
  const index = Number(q.get("index") || 0);
  const version = getVersion(q.get("version"));

  if (!url) return errorJson("Parameter 'url' is required", 400);
  if (!isTiktokUrl(url)) return errorJson("Only TikTok/Douyin URLs are supported", 400);

  let target;
  try {
    target = await resolveDownload(url, type, index, version);
  } catch (err: any) {
    const msg = err?.message || "Failed to resolve media";
    return errorJson(msg, 404);
  }

  try {
    const clientRange = request.headers.get("range");

    const buildHeaders = (range: string) => {
      const h: Record<string, string> = {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        Referer: "https://www.tiktok.com/",
      };
      if (range) h["Range"] = range;
      return h;
    };

    let upstream = await fetch(target.url, {
      headers: buildHeaders(clientRange || ""),
      redirect: "follow",
    });

    if (upstream.status === 416 && clientRange) {
      upstream = await fetch(target.url, {
        headers: buildHeaders(""),
        redirect: "follow",
      });
    }

    if (!upstream.ok && upstream.status !== 206) {
      return errorJson(`Upstream CDN returned ${upstream.status}`, 502);
    }

    const encoded = encodeURIComponent(target.filename);
    const headers = new Headers();
    headers.set("Content-Type", target.contentType);
    headers.set(
      "Content-Disposition",
      `attachment; filename="${encoded}"; filename*=UTF-8''${encoded}`
    );
    headers.set("X-Filename", encoded);
    const cl = upstream.headers.get("content-length");
    if (cl) headers.set("Content-Length", cl);
    const cr = upstream.headers.get("content-range");
    if (cr) headers.set("Content-Range", cr);
    const ae = upstream.headers.get("accept-ranges");
    if (ae) headers.set("Accept-Ranges", ae);
    for (const [k, v] of Object.entries(CORS_HEADERS)) headers.set(k, v);

    return new Response(upstream.body, {
      status: upstream.status,
      headers,
    });
  } catch (err: any) {
    return errorJson(err?.message || "Failed to stream media from CDN", 502);
  }
}

const server = Bun.serve({
  port: PORT,
  async fetch(request) {
    const { pathname } = new URL(request.url);
    const method = request.method;

    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    try {
      if (pathname === "/health") return await handleHealth();

      if (pathname === "/fetch" && (method === "GET" || method === "POST")) {
        return await handleFetch(request);
      }

      if (pathname === "/download" && method === "GET") {
        return await handleDownload(request);
      }

      return errorJson("Route not found", 404);
    } catch (err: any) {
      console.error("Unhandled error:", err);
      return errorJson(err?.message || "Unexpected server error", 500);
    }
  },
});

console.log(`tiktok-api-dl-server listening on http://localhost:${server.port}`);
console.log(`Default downloader version: ${DEFAULT_VERSION}`);
console.log(`Endpoints:`);
console.log(`  GET  /health`);
console.log(`  GET|POST /fetch?url=<tiktok>[&version=v1|v2|v3]`);
console.log(`  GET  /download?url=<tiktok>&type=video|image|music[&index=0][&version=v1]`);
