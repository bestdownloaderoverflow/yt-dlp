import { mkdir, rm, writeFile, readFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

const FFMPEG_TIMEOUT_MS = 90_000;
const DEFAULT_DURATION_PER_IMAGE = 4;

const CDN_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  Referer: "https://www.tiktok.com/",
};

export interface SlideshowOptions {
  durationPerImage?: number;
  ffmpegPath?: string;
}

export class SlideshowError extends Error {
  status: number;
  constructor(message: string, status = 502) {
    super(message);
    this.status = status;
  }
}

async function downloadToFile(url: string, dest: string): Promise<void> {
  const resp = await fetch(url, {
    headers: CDN_HEADERS,
    redirect: "follow",
  });
  if (!resp.ok) {
    throw new SlideshowError(
      `Failed to download ${url.slice(0, 60)}...: HTTP ${resp.status}`,
      resp.status >= 400 && resp.status < 500 ? 404 : 502
    );
  }
  const buf = await resp.arrayBuffer();
  await writeFile(dest, Buffer.from(buf));
}

export async function renderSlideshow(
  photoUrls: string[],
  audioUrl: string | null | undefined,
  options: SlideshowOptions = {}
): Promise<{ outputPath: string; tempDir: string }> {
  if (!photoUrls || photoUrls.length === 0) {
    throw new SlideshowError("No photos available for slideshow", 400);
  }

  const ffmpegPath = options.ffmpegPath || "ffmpeg";
  const durationPerImage = options.durationPerImage || DEFAULT_DURATION_PER_IMAGE;

  const tempDir = await mkdir(join(tmpdir(), `tiktok_slideshow_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`), {
    recursive: true,
  }).then(() => join(tmpdir(), `tiktok_slideshow_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`));

  // Re-create because join above computed path before mkdir resolved
  const actualTempDir = tempDir;
  await mkdir(actualTempDir, { recursive: true });

  // Download all images sequentially to bound concurrency
  const imagePaths: string[] = [];
  for (let i = 0; i < photoUrls.length; i++) {
    const dst = join(actualTempDir, `image_${i}.jpg`);
    await downloadToFile(photoUrls[i], dst);
    imagePaths.push(dst);
  }

  let audioPath: string | null = null;
  if (audioUrl && audioUrl.trim() !== "") {
    audioPath = join(actualTempDir, "audio.mp3");
    await downloadToFile(audioUrl, audioPath);
  }

  const outputPath = join(actualTempDir, "slideshow.mp4");

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
  filterParts.push(
    `${concatInputs.join("")}concat=n=${imagePaths.length}:v=1:a=0[vout]`
  );

  if (audioPath) {
    const videoDuration = imagePaths.length * durationPerImage;
    filterParts.push(
      `[${audioInputIndex}:a]atrim=0:${videoDuration},asetpts=PTS-STARTPTS[aout]`
    );
    args.push(
      "-filter_complex",
      filterParts.join(";"),
      "-map",
      "[vout]",
      "-map",
      "[aout]",
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
      "1024",
      "-c:a",
      "aac",
      "-b:a",
      "128k",
      outputPath
    );
  } else {
    args.push(
      "-filter_complex",
      filterParts.join(";"),
      "-map",
      "[vout]",
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
      "1024",
      outputPath
    );
  }

  const proc = Bun.spawn([ffmpegPath, ...args], {
    stdout: "pipe",
    stderr: "pipe",
  });

  const exitPromise = proc.exited;
  const timeoutPromise = new Promise<{ code: number | null; timedOut: true }>(
    (resolve) => {
      setTimeout(() => {
        try {
          proc.kill();
        } catch {
          // already exited
        }
        resolve({ code: null, timedOut: true });
      }, FFMPEG_TIMEOUT_MS);
    }
  );

  const result = await Promise.race([
    exitPromise.then((code) => ({ code, timedOut: false as const })),
    timeoutPromise,
  ]);

  if (result.timedOut) {
    throw new SlideshowError(
      `ffmpeg timed out after ${FFMPEG_TIMEOUT_MS / 1000}s`,
      504
    );
  }
  if (result.code !== 0) {
    const stderrStr = await new Response(proc.stderr).text();
    throw new SlideshowError(
      `ffmpeg failed (exit ${result.code}): ${stderrStr.trim().slice(0, 400)}`,
      502
    );
  }

  return { outputPath, tempDir: actualTempDir };
}

export async function cleanupTemp(tempDir: string): Promise<void> {
  try {
    await rm(tempDir, { recursive: true, force: true });
  } catch {
    // best-effort
  }
}

export async function readSlideshowFile(
  outputPath: string
): Promise<Buffer> {
  return readFile(outputPath);
}
