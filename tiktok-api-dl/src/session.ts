import { randomUUID } from "node:crypto";
import Redis from "ioredis";

export type SessionType = "video" | "photo" | "mp3" | "slideshow";

export interface SessionData {
  key: string;
  url: string;
  type: SessionType;
  quality?: string;
  direct_url?: string;
  http_headers?: Record<string, string>;
  cookies?: string;
  author?: string;
  platform: string;
  proxy?: string;
  impersonate?: string;
  filesize?: number;
  duration?: number;
  photo_index?: number;
  photo_urls?: string[];
  audio_url?: string;
  title?: string;
  createdAt: number;
  expiresAt: number;
}

const DEFAULT_TTL_SECONDS = 300;
const DEFAULT_TTL_MS = DEFAULT_TTL_SECONDS * 1000;
const REDIS_KEY_PREFIX = "tiktok:session:";

const REDIS_URL = process.env.REDIS_URL || "";

interface SessionBackend {
  create(data: Omit<SessionData, "key" | "createdAt" | "expiresAt">, ttlSeconds: number): Promise<string>;
  get(key: string): Promise<SessionData | null>;
  del(key: string): Promise<void>;
  count(): Promise<number>;
  close(): Promise<void>;
}

// ---------- In-memory backend (fallback ketika REDIS_URL tidak diset) ----------
class MemoryBackend implements SessionBackend {
  private sessions = new Map<string, { data: SessionData; timeoutHandle?: ReturnType<typeof setTimeout> }>();

  async create(data: Omit<SessionData, "key" | "createdAt" | "expiresAt">, ttlSeconds: number): Promise<string> {
    const key = generateKey();
    const now = Date.now();
    const session: SessionData = { ...data, key, createdAt: now, expiresAt: now + ttlSeconds * 1000 };
    const entry = { data: session };
    entry.timeoutHandle = setTimeout(() => {
      this.sessions.delete(key);
    }, ttlSeconds * 1000);
    this.sessions.set(key, entry);
    return key;
  }

  async get(key: string): Promise<SessionData | null> {
    if (!key) return null;
    const entry = this.sessions.get(key);
    if (!entry) return null;
    if (Date.now() >= entry.data.expiresAt) {
      if (entry.timeoutHandle) clearTimeout(entry.timeoutHandle);
      this.sessions.delete(key);
      return null;
    }
    return entry.data;
  }

  async del(key: string): Promise<void> {
    const entry = this.sessions.get(key);
    if (entry?.timeoutHandle) clearTimeout(entry.timeoutHandle);
    this.sessions.delete(key);
  }

  async count(): Promise<number> {
    return this.sessions.size;
  }

  async close(): Promise<void> {}
}

// ---------- Redis backend ----------
class RedisBackend implements SessionBackend {
  private client: Redis;
  private connected = false;
  private connectPromise: Promise<void> | null = null;

  constructor(url: string) {
    this.client = new Redis(url, {
      maxRetriesPerRequest: 3,
      enableReadyCheck: true,
      lazyConnect: false,
      reconnectOnError(err) {
        const targets = [/READONLY/, /ETIMEDOUT/, /ECONNRESET/];
        return targets.some((t) => t.test(err.message));
      },
    });

    this.client.on("connect", () => {
      this.connected = true;
      console.log("[session] Redis connected:", maskUrl(url));
    });
    this.client.on("error", (err) => {
      console.error("[session] Redis error:", err.message);
    });
    this.client.on("close", () => {
      this.connected = false;
    });
  }

  private rkey(key: string): string {
    return `${REDIS_KEY_PREFIX}${key}`;
  }

  async create(data: Omit<SessionData, "key" | "createdAt" | "expiresAt">, ttlSeconds: number): Promise<string> {
    const key = generateKey();
    const now = Date.now();
    const session: SessionData = { ...data, key, createdAt: now, expiresAt: now + ttlSeconds * 1000 };
    const payload = JSON.stringify(session);
    await this.client.set(this.rkey(key), payload, "EX", ttlSeconds);
    return key;
  }

  async get(key: string): Promise<SessionData | null> {
    if (!key) return null;
    const raw = await this.client.get(this.rkey(key));
    if (!raw) return null;
    try {
      return JSON.parse(raw) as SessionData;
    } catch {
      await this.client.del(this.rkey(key));
      return null;
    }
  }

  async del(key: string): Promise<void> {
    await this.client.del(this.rkey(key));
  }

  async count(): Promise<number> {
    const keys = await this.client.keys(`${REDIS_KEY_PREFIX}*`);
    return keys.length;
  }

  async close(): Promise<void> {
    try {
      await this.client.quit();
    } catch {
      this.client.disconnect();
    }
  }
}

function maskUrl(url: string): string {
  return url.replace(/(:[^@]+@)/, ":***@");
}

let backend: SessionBackend | null = null;

function getBackend(): SessionBackend {
  if (backend) return backend;
  if (REDIS_URL) {
    backend = new RedisBackend(REDIS_URL);
    console.log("[session] Using Redis backend");
  } else {
    backend = new MemoryBackend();
    console.log("[session] Using in-memory backend (set REDIS_URL to enable Redis)");
  }
  return backend;
}

export async function createSession(
  data: Omit<SessionData, "key" | "createdAt" | "expiresAt">,
  ttlSeconds: number = DEFAULT_TTL_SECONDS
): Promise<string> {
  return getBackend().create(data, ttlSeconds);
}

export async function getSession(key: string): Promise<SessionData | null> {
  return getBackend().get(key);
}

export async function deleteSession(key: string): Promise<void> {
  return getBackend().del(key);
}

export async function activeSessionCount(): Promise<number> {
  return getBackend().count();
}

export async function closeSessionStore(): Promise<void> {
  if (backend) {
    await backend.close();
    backend = null;
  }
}

export function isRedisBackend(): boolean {
  return backend instanceof RedisBackend;
}

function generateKey(): string {
  const raw = randomUUID().replace(/-/g, "");
  return raw.slice(0, 22);
}
