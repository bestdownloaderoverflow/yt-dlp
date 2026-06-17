import { createHash } from "node:crypto";
import Redis from "ioredis";
import type { ExtractOptions } from "./tiktok.ts";

const REDIS_URL = process.env.REDIS_URL || "";
const EXTRACT_CACHE_TTL = Number(process.env.TIKTOK_EXTRACT_CACHE_TTL_SECONDS || "1800");

const LOCK_TTL_SECONDS = 35;
const STAMPEDE_WAIT_MS = 8000;
const POLL_INTERVAL_MS = 300;

const KEY_PREFIX = "exinfo:";
const LOCK_PREFIX = "exlock:";

function maskUrl(url: string): string {
  return url.replace(/(:[^@]+@)/, ":***@");
}

function buildRawKey(url: string, options: ExtractOptions): string {
  const raw = `${url}|${options.proxy || ""}|${options.impersonate || ""}|${options.version || ""}`;
  return raw;
}

function buildCacheKey(url: string, options: ExtractOptions): string {
  return KEY_PREFIX + createHash("sha256").update(buildRawKey(url, options)).digest("hex");
}

function buildLockKey(url: string, options: ExtractOptions): string {
  return LOCK_PREFIX + createHash("sha256").update(buildRawKey(url, options)).digest("hex");
}

interface CacheBackend {
  get(key: string): Promise<string | null>;
  set(key: string, value: string, ttlSeconds: number): Promise<void>;
  setLock(key: string, ttlSeconds: number): Promise<boolean>;
  del(key: string): Promise<void>;
  close(): Promise<void>;
}

class MemoryCacheBackend implements CacheBackend {
  private store = new Map<string, { value: string; expiresAt: number }>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();

  async get(key: string): Promise<string | null> {
    const entry = this.store.get(key);
    if (!entry) return null;
    if (Date.now() >= entry.expiresAt) {
      this.evict(key);
      return null;
    }
    return entry.value;
  }

  async set(key: string, value: string, ttlSeconds: number): Promise<void> {
    this.evict(key);
    const expiresAt = Date.now() + ttlSeconds * 1000;
    this.store.set(key, { value, expiresAt });
    this.timers.set(key, setTimeout(() => this.evict(key), ttlSeconds * 1000));
  }

  async setLock(key: string, ttlSeconds: number): Promise<boolean> {
    const entry = this.store.get(key);
    if (entry && Date.now() < entry.expiresAt) return false;
    await this.set(key, "1", ttlSeconds);
    return true;
  }

  async del(key: string): Promise<void> {
    this.evict(key);
  }

  private evict(key: string): void {
    const t = this.timers.get(key);
    if (t) clearTimeout(t);
    this.timers.delete(key);
    this.store.delete(key);
  }

  async close(): Promise<void> {
    for (const t of this.timers.values()) clearTimeout(t);
    this.timers.clear();
    this.store.clear();
  }
}

class RedisCacheBackend implements CacheBackend {
  private client: Redis;

  constructor(url: string) {
    this.client = new Redis(url, {
      maxRetriesPerRequest: 3,
      enableReadyCheck: true,
      lazyConnect: false,
      reconnectOnError(err) {
        return [/READONLY/, /ETIMEDOUT/, /ECONNRESET/].some((t) => t.test(err.message));
      },
    });
    this.client.on("connect", () => {
      console.log("[extract-cache] Redis connected:", maskUrl(url));
    });
    this.client.on("error", (err) => {
      console.error("[extract-cache] Redis error:", err.message);
    });
  }

  async get(key: string): Promise<string | null> {
    return this.client.get(key);
  }

  async set(key: string, value: string, ttlSeconds: number): Promise<void> {
    await this.client.set(key, value, "EX", ttlSeconds);
  }

  async setLock(key: string, ttlSeconds: number): Promise<boolean> {
    const res = await this.client.set(key, "1", "NX", "EX", ttlSeconds);
    return res === "OK";
  }

  async del(key: string): Promise<void> {
    await this.client.del(key);
  }

  async close(): Promise<void> {
    try {
      await this.client.quit();
    } catch {
      this.client.disconnect();
    }
  }
}

let backend: CacheBackend | null = null;
let useRedis = false;

function getBackend(): CacheBackend {
  if (backend) return backend;
  if (REDIS_URL) {
    backend = new RedisCacheBackend(REDIS_URL);
    useRedis = true;
    console.log("[extract-cache] Using Redis backend");
  } else {
    backend = new MemoryCacheBackend();
    useRedis = false;
    console.log("[extract-cache] Using in-memory backend (set REDIS_URL to enable Redis)");
  }
  return backend;
}

export function isExtractCacheRedis(): boolean {
  return useRedis;
}

export function extractCacheTtl(): number {
  return EXTRACT_CACHE_TTL;
}

export async function getOrExtract(
  url: string,
  options: ExtractOptions,
  extractFn: () => Promise<any>
): Promise<any> {
  const b = getBackend();
  const ckey = buildCacheKey(url, options);
  const lkey = buildLockKey(url, options);

  try {
    const cached = await b.get(ckey);
    if (cached) return JSON.parse(cached);
  } catch (err: any) {
    console.error("[extract-cache] get error:", err?.message);
  }

  let acquired = false;
  try {
    acquired = await b.setLock(lkey, LOCK_TTL_SECONDS);
  } catch (err: any) {
    console.error("[extract-cache] setLock error:", err?.message);
  }

  if (!acquired) {
    const deadline = Date.now() + STAMPEDE_WAIT_MS;
    while (Date.now() < deadline) {
      await sleep(POLL_INTERVAL_MS);
      try {
        const data = await b.get(ckey);
        if (data) return JSON.parse(data);
      } catch {
        break;
      }
    }
  }

  try {
    const raw = await extractFn();
    try {
      await b.set(ckey, JSON.stringify(raw), EXTRACT_CACHE_TTL);
    } catch (err: any) {
      console.error("[extract-cache] set error:", err?.message);
    }
    return raw;
  } finally {
    if (acquired) {
      try {
        await b.del(lkey);
      } catch {
        /* noop */
      }
    }
  }
}

export async function invalidateExtractCache(url: string, options: ExtractOptions): Promise<void> {
  if (!backend) return;
  try {
    await backend.del(buildCacheKey(url, options));
  } catch {
    /* noop */
  }
}

export async function closeExtractionCache(): Promise<void> {
  if (backend) {
    await backend.close();
    backend = null;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
