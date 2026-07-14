import { createWriteStream } from "node:fs";
import { mkdir, rm, stat } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";

const FFMPEG_TIMEOUT_MS = 90_000;
const DEFAULT_DURATION_PER_IMAGE = 4;
const MAX_PHOTOS = Number(process.env.SLIDESHOW_MAX_PHOTOS) || 35;
const MAX_FILE_BYTES = Number(process.env.SLIDESHOW_MAX_FILE_BYTES) || 15 * 1024 * 1024;
const MAX_TOTAL_BYTES = Number(process.env.SLIDESHOW_MAX_TOTAL_BYTES) || 80 * 1024 * 1024;
const MAX_CONCURRENT = Number(process.env.SLIDESHOW_MAX_CONCURRENT) || 1;

const CDN_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  Referer: "https://www.tiktok.com/",
};

export interface SlideshowOptions {
  durationPerImage?: number;
  ffmpegPath?: string;
  signal?: AbortSignal;
}

export class SlideshowError extends Error {
  status: number;
  constructor(message: string, status = 502) {
    super(message);
    this.status = status;
  }
}

let activeSlideshows = 0;
const waitQueue: Array<() => void> = [];

async function acquireSlideshowSlot(signal?: AbortSignal): Promise<() => void> {
  if (activeSlideshows < MAX_CONCURRENT) {
    activeSlideshows++;
    return () => {
      activeSlideshows = Math.max(0, activeSlideshows - 1);
      const next = waitQueue.shift();
      if (next) next();
    };
  }
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      const idx = waitQueue.indexOf(resolve);
      if (idx >= 0) waitQueue.splice(idx, 1);
      reject(new SlideshowError("Slideshow cancelled", 499));
    };
    if (signal?.aborted) {
      reject(new SlideshowError("Slideshow cancelled", 499));
      return;
    }
    waitQueue.push(resolve);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
  activeSlideshows++;
  return () => {
    activeSlideshows = Math.max(0, activeSlideshows - 1);
    const next = waitQueue.shift();
    if (next) next();
  };
}

export function activeSlideshowCount(): number {
  return activeSlideshows;
}

function assertNotAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new SlideshowError("Slideshow cancelled", 499);
}

function byteLimitTransform(maxBytes: number, onBytes: (n: number) => void): Transform {
  let total = 0;
  return new Transform({
    transform(chunk, _enc, cb) {
      total += chunk.byteLength ?? (chunk as Buffer).length;
      if (total > maxBytes) {
        cb(new SlideshowError(`Upstream asset exceeds ${maxBytes} bytes`, 413));
        return;
      }
      onBytes(chunk.byteLength ?? (chunk as Buffer).length);
      cb(null, chunk);
    },
  });
}

async function downloadToFile(
  url: string,
  dest: string,
  budget: { used: number },
  signal?: AbortSignal
): Promise<void> {
  assertNotAborted(signal);
  const resp = await fetch(url, {
    headers: CDN_HEADERS,
    redirect: "follow",
    signal,
  });
  if (!resp.ok) {
    throw new SlideshowError(
      `Failed to download ${url.slice(0, 60)}...: HTTP ${resp.status}`,
      resp.status >= 400 && resp.status < 500 ? 404 : 502
    );
  }
  if (!resp.body) {
    throw new SlideshowError("Upstream CDN returned an empty body", 502);
  }
  const cl = resp.headers.get("content-length");
  if (cl) {
    const n = Number(cl);
    if (Number.isFinite(n) && n > MAX_FILE_BYTES) {
      throw new SlideshowError(`Upstream asset Content-Length ${n} exceeds limit`, 413);
    }
    if (Number.isFinite(n) && budget.used + n > MAX_TOTAL_BYTES) {
      throw new SlideshowError("Slideshow download budget exceeded", 413);
    }
  }

  const nodeStream = Readable.fromWeb(resp.body as any);
  const limiter = byteLimitTransform(MAX_FILE_BYTES, (n) => {
    budget.used += n;
    if (budget.used > MAX_TOTAL_BYTES) {
      nodeStream.destroy(new SlideshowError("Slideshow download budget exceeded", 413));
    }
  });
  await pipeline(nodeStream, limiter, createWriteStream(dest));
}

function makeTempDirName(): string {
  return join(tmpdir(), `tiktok_slideshow_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
}

async function runFfmpeg(ffmpegPath: string, args: string[], signal?: AbortSignal): Promise<void> {
  assertNotAborted(signal);

  const proc = spawn(ffmpegPath, args, {
    stdio: ["ignore", "ignore", "pipe"],
  }) as ChildProcessWithoutNullStreams;

  let stderr = "";
  proc.stderr?.on("data", (chunk: Buffer | string) => {
    if (stderr.length < 4096) {
      stderr += typeof chunk === "string" ? chunk : chunk.toString("utf8");
    }
  });

  let timedOut = false;
  let killedByAbort = false;

  const killProc = () => {
    try {
      proc.kill("SIGKILL");
    } catch {
      /* already exited */
    }
  };

  const onAbort = () => {
    killedByAbort = true;
    killProc();
  };
  signal?.addEventListener("abort", onAbort, { once: true });

  const timer = setTimeout(() => {
    timedOut = true;
    killProc();
  }, FFMPEG_TIMEOUT_MS);

  try {
    const code: number | null = await new Promise((resolve, reject) => {
      proc.on("error", reject);
      proc.on("close", (exitCode) => resolve(exitCode));
    });

    if (killedByAbort || signal?.aborted) {
      throw new SlideshowError("Slideshow cancelled", 499);
    }
    if (timedOut) {
      throw new SlideshowError(`ffmpeg timed out after ${FFMPEG_TIMEOUT_MS / 1000}s`, 504);
    }
    if (code !== 0) {
      throw new SlideshowError(
        `ffmpeg failed (exit ${code}): ${stderr.trim().slice(0, 400)}`,
        502
      );
    }
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

function buildEncodeArgs(
  imagePaths: string[],
  audioPath: string | null,
  durationPerImage: number,
  outputPath: string
): string[] {
  const args: string[] = ["-y", "-hide_banner", "-loglevel", "error"];
  for (const img of imagePaths) {
    args.push("-loop", "1", "-t", String(durationPerImage), "-i", img);
  }
  let audioInputIndex = -1;
  if (audioPath) {
    audioInputIndex = imagePaths.length;
    args.push("-stream_loop", "-1", "-i", audioPath);
  }

  const filterParts: string[] = [];
  const concatInputs: string[] = [];
  for (let i = 0; i < imagePaths.length; i++) {
    filterParts.push(
      `[${i}:v]scale=w=720:h=1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=24,trim=duration=${durationPerImage},setpts=PTS-STARTPTS[v${i}]`
    );
    concatInputs.push(`[v${i}]`);
  }
  filterParts.push(`${concatInputs.join("")}concat=n=${imagePaths.length}:v=1:a=0[vout]`);

  if (audioPath) {
    const videoDuration = imagePaths.length * durationPerImage;
    filterParts.push(`[${audioInputIndex}:a]atrim=0:${videoDuration},asetpts=PTS-STARTPTS[aout]`);
  }

  args.push("-filter_complex", filterParts.join(";"), "-map", "[vout]");
  if (audioPath) {
    args.push("-map", "[aout]");
  }

  args.push(
    "-pix_fmt",
    "yuv420p",
    "-fps_mode",
    "cfr",
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-tune",
    "stillimage",
    "-crf",
    "28",
    "-b:v",
    "320k",
    "-maxrate",
    "360k",
    "-bufsize",
    "720k",
    "-threads",
    "1",
    "-max_muxing_queue_size",
    "1024"
  );

  if (audioPath) {
    args.push("-c:a", "aac", "-b:a", "128k");
  }

  args.push(outputPath);
  return args;
}

export async function renderSlideshow(
  photoUrls: string[],
  audioUrl: string | null | undefined,
  options: SlideshowOptions = {}
): Promise<{ outputPath: string; tempDir: string }> {
  if (!photoUrls || photoUrls.length === 0) {
    throw new SlideshowError("No photos available for slideshow", 400);
  }
  if (photoUrls.length > MAX_PHOTOS) {
    throw new SlideshowError(`Too many photos (max ${MAX_PHOTOS})`, 400);
  }

  const release = await acquireSlideshowSlot(options.signal);
  const ffmpegPath = options.ffmpegPath || "ffmpeg";
  const durationPerImage = options.durationPerImage || DEFAULT_DURATION_PER_IMAGE;
  const signal = options.signal;

  const tempDir = makeTempDirName();
  await mkdir(tempDir, { recursive: true });

  try {
    assertNotAborted(signal);
    const budget = { used: 0 };
    const imagePaths: string[] = [];
    for (let i = 0; i < photoUrls.length; i++) {
      const dst = join(tempDir, `image_${i}.jpg`);
      await downloadToFile(photoUrls[i], dst, budget, signal);
      imagePaths.push(dst);
    }

    let audioPath: string | null = null;
    if (audioUrl && audioUrl.trim() !== "") {
      audioPath = join(tempDir, "audio.mp3");
      await downloadToFile(audioUrl, audioPath, budget, signal);
    }

    const outputPath = join(tempDir, "slideshow.mp4");
    const args = buildEncodeArgs(imagePaths, audioPath, durationPerImage, outputPath);
    await runFfmpeg(ffmpegPath, args, signal);
    return { outputPath, tempDir };
  } catch (err) {
    await cleanupTemp(tempDir);
    throw err;
  } finally {
    release();
  }
}

export async function cleanupTemp(tempDir: string): Promise<void> {
  try {
    await rm(tempDir, { recursive: true, force: true });
  } catch {
    /* best-effort */
  }
}

export async function slideshowFileSize(outputPath: string): Promise<number> {
  const s = await stat(outputPath);
  return s.size;
}
