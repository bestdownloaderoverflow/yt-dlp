import Tiktok from "@tobyg74/tiktok-api-dl";
import { createSession } from "./session.ts";

export type DownloaderVersion = "v1" | "v2" | "v3";

export interface ExtractOptions {
  version?: DownloaderVersion;
  proxy?: string;
  impersonate?: string;
}

export interface AuthorInfo {
  nickname: string;
  uniqueId: string;
  signature: string;
  avatar: string;
  avatarThumb: string;
  avatarMedium: string;
  avatarLarger: string;
}

export interface StatisticsInfo {
  play_count: number;
  digg_count: number;
  comment_count: number;
  share_count: number;
}

export interface PhotoItem {
  type: "photo";
  url: string;
  download_link: string;
}

export interface TikTokVideoResponse {
  status: "tunnel";
  extract_source: string | null;
  title: string;
  description: string;
  statistics: StatisticsInfo;
  artist: string;
  cover: string;
  duration: number;
  audio: string | null;
  download_link: {
    watermark?: string;
    no_watermark?: string;
    no_watermark_hd?: string;
    mp3?: string;
  };
  music_duration: number;
  author: AuthorInfo;
}

export interface TikTokPhotoResponse {
  status: "picker";
  extract_source: string | null;
  title: string;
  description: string;
  statistics: StatisticsInfo;
  artist: string;
  cover: string;
  duration: number;
  audio: string | null;
  download_link: {
    no_watermark: string[];
    mp3?: string;
  };
  photos: PhotoItem[];
  download_slideshow: string;
  author: AuthorInfo;
}

export type TikTokResponse = TikTokVideoResponse | TikTokPhotoResponse;

const EXTRACT_SOURCE_BY_VERSION: Record<DownloaderVersion, string> = {
  v1: "web",
  v2: "ssstik",
  v3: "musicaldown",
};

function firstUrl(value: any): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.length > 0) {
    const item = value[0];
    return typeof item === "string" ? item : item?.url || "";
  }
  if (value.url) return value.url;
  return "";
}

function num(value: any): number {
  if (typeof value === "number" && isFinite(value)) return value;
  return 0;
}

function buildAuthor(r: any): AuthorInfo {
  const nickname = r?.author?.nickname || r?.author?.username || "unknown";
  const uniqueId = r?.author?.username || r?.author?.nickname || "unknown";
  const signature = r?.author?.signature || "";
  const avatar =
    firstUrl(r?.author?.avatarThumb) ||
    firstUrl(r?.author?.avatarMedium) ||
    r?.author?.avatar ||
    "";
  return {
    nickname,
    uniqueId,
    signature,
    avatar,
    avatarThumb: avatar,
    avatarMedium: avatar,
    avatarLarger: avatar,
  };
}

function buildStatistics(r: any): StatisticsInfo {
  const s = r?.statistics || {};
  return {
    play_count: num(s.playCount),
    digg_count: num(s.likeCount),
    comment_count: num(s.commentCount),
    share_count: num(s.shareCount),
  };
}

export async function extractPost(
  url: string,
  options: ExtractOptions = {}
): Promise<TikTokResponse> {
  const version = options.version || "v1";

  const response: any = await (Tiktok as any).Downloader(url, {
    version,
    ...(options.proxy ? { proxy: options.proxy } : {}),
  });

  if (!response || response.status === "error") {
    throw new Error(response?.message || "Tiktok.Downloader returned an error");
  }

  const r = response.result;
  if (!r) throw new Error("No result returned from downloader");

  const author = buildAuthor(r);
  const statistics = buildStatistics(r);
  const durationMs = r?.video?.duration ? num(r.video.duration) : 0;
  const cover = firstUrl(r?.cover) || firstUrl(r?.video?.cover) || "";
  const title = r?.desc || r?.title || "";
  const description = r?.desc || "";
  const audioUrl = firstUrl(r?.music?.playUrl) || r?.music?.playUrl || null;
  const safeAuthor = sanitizeFilenamePart(author.nickname, "tiktok");

  const isPhoto = r.type === "image" || (Array.isArray(r?.images) && r.images.length > 0);

  if (isPhoto) {
    // Photo / slideshow
    const imageUrls: string[] = Array.isArray(r?.images) ? r.images.filter(Boolean) : [];
    const photoKeys: string[] = [];
    const photos: PhotoItem[] = [];

    for (let i = 0; i < imageUrls.length; i++) {
      const key = await createSession({
        url,
        type: "photo",
        photo_index: i + 1,
        direct_url: imageUrls[i],
        http_headers: {},
        author: safeAuthor,
        platform: "tiktok",
        proxy: options.proxy,
        impersonate: options.impersonate,
        duration: durationMs,
      });
      const link = `/tiktok/download?key=${key}`;
      photoKeys.push(link);
      photos.push({ type: "photo", url: imageUrls[i], download_link: link });
    }

    const links: TikTokPhotoResponse["download_link"] = {
      no_watermark: photoKeys,
    };

    if (audioUrl) {
      const mp3Key = await createSession({
        url,
        type: "mp3",
        direct_url: audioUrl,
        http_headers: {},
        author: safeAuthor,
        platform: "tiktok",
        proxy: options.proxy,
        impersonate: options.impersonate,
        duration: durationMs,
      });
      links.mp3 = `/tiktok/download?key=${mp3Key}`;
    }

    const slideshowKey = await createSession({
      url,
      type: "slideshow",
      photo_urls: imageUrls,
      audio_url: audioUrl || undefined,
      author: safeAuthor,
      platform: "tiktok",
      proxy: options.proxy,
      impersonate: options.impersonate,
      duration: durationMs || imageUrls.length * 4,
    });
    const slideshowLink = `/tiktok/download?key=${slideshowKey}`;

    return {
      status: "picker",
      extract_source: EXTRACT_SOURCE_BY_VERSION[version],
      title,
      description,
      statistics,
      artist: author.nickname,
      cover,
      duration: durationMs,
      audio: audioUrl,
      download_link: links,
      photos,
      download_slideshow: slideshowLink,
      author,
    };
  }

  // Video post
  const links: TikTokVideoResponse["download_link"] = {};

  // Build candidate video URLs (v1 has playAddr/downloadAddr; v3 has videoHD/videoWatermark)
  const candidates: { url: string; quality: "watermark" | "no_watermark" | "no_watermark_hd" }[] = [];

  if (version === "v1") {
    const playAddr = firstUrl(r?.video?.playAddr);
    const downloadAddr = firstUrl(r?.video?.downloadAddr);
    if (playAddr) candidates.push({ url: playAddr, quality: "no_watermark_hd" });
    if (downloadAddr) candidates.push({ url: downloadAddr, quality: "watermark" });
  } else if (version === "v2") {
    const v = r?.video?.playAddr || r?.direct;
    if (v) candidates.push({ url: v, quality: "no_watermark" });
  } else if (version === "v3") {
    if (r?.videoHD) candidates.push({ url: r.videoHD, quality: "no_watermark_hd" });
    if (r?.videoWatermark) candidates.push({ url: r.videoWatermark, quality: "watermark" });
  }

  // Deduplicate by quality (first wins)
  const seen = new Set<string>();
  for (const c of candidates) {
    if (seen.has(c.quality)) continue;
    seen.add(c.quality);
    const key = await createSession({
      url,
      type: "video",
      quality: c.quality,
      direct_url: c.url,
      http_headers: {},
      author: safeAuthor,
      platform: "tiktok",
      proxy: options.proxy,
      impersonate: options.impersonate,
      duration: durationMs,
    });
    links[c.quality] = `/tiktok/download?key=${key}`;
  }

  if (audioUrl) {
    const mp3Key = await createSession({
      url,
      type: "mp3",
      direct_url: audioUrl,
      http_headers: {},
      author: safeAuthor,
      platform: "tiktok",
      proxy: options.proxy,
      impersonate: options.impersonate,
      duration: durationMs,
    });
    links.mp3 = `/tiktok/download?key=${mp3Key}`;
  }

  return {
    status: "tunnel",
    extract_source: EXTRACT_SOURCE_BY_VERSION[version],
    title,
    description,
    statistics,
    artist: author.nickname,
    cover,
    duration: durationMs,
    audio: audioUrl,
    download_link: links,
    music_duration: durationMs,
    author,
  };
}

export function isTiktokUrl(url: string): boolean {
  return /tiktok\.com|douyin\.com|vt\.tiktok|vm\.tiktok/.test(url);
}

export function sanitizeFilenamePart(value: string | undefined | null, fallback: string): string {
  if (!value) return fallback;
  const cleaned = value.replace(/[^a-zA-Z0-9]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "");
  return cleaned || fallback;
}
