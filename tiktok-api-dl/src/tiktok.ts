import Tiktok from "@tobyg74/tiktok-api-dl";

export type DownloaderVersion = "v1" | "v2" | "v3";
export type MediaType = "video" | "image" | "music";

export interface FetchResult {
  status: "success";
  version: DownloaderVersion;
  type: "video" | "image";
  id: string;
  description: string;
  author: {
    username: string;
    nickname: string;
    avatar: string;
  };
  statistics?: Record<string, number>;
  cover?: string;
  duration?: number;
  download: {
    video: { url: string; quality?: string }[];
    images: string[];
    music: { url: string; title?: string; author?: string }[];
  };
  raw: any;
}

export interface FetchOptions {
  version?: DownloaderVersion;
  proxy?: string;
}

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

export async function fetchPost(
  url: string,
  options: FetchOptions = {}
): Promise<FetchResult> {
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

  const download = {
    video: [] as { url: string; quality?: string }[],
    images: [] as string[],
    music: [] as { url: string; title?: string; author?: string }[],
  };

  // v1 (TiktokAPIResponse)
  if (version === "v1") {
    const playAddr = firstUrl(r?.video?.playAddr) || firstUrl(r?.video?.downloadAddr);
    if (playAddr) download.video.push({ url: playAddr });
    if (Array.isArray(r?.images)) download.images = r.images.filter(Boolean);
    const musicUrl = firstUrl(r?.music?.playUrl);
    if (musicUrl) {
      download.music.push({
        url: musicUrl,
        title: r?.music?.title,
        author: r?.music?.author,
      });
    }
  }
  // v2 (SSSTikResponse)
  else if (version === "v2") {
    const v = r?.video?.playAddr || r?.direct;
    if (v) download.video.push({ url: v });
    if (Array.isArray(r?.images)) download.images = r.images.filter(Boolean);
    const m = r?.music?.playUrl;
    if (m) download.music.push({ url: m });
  }
  // v3 (MusicalDownResponse)
  else if (version === "v3") {
    if (r?.videoHD) download.video.push({ url: r.videoHD, quality: "HD" });
    if (r?.videoWatermark) download.video.push({ url: r.videoWatermark, quality: "watermark" });
    if (Array.isArray(r?.images)) download.images = r.images.filter(Boolean);
    if (r?.music) download.music.push({ url: r.music });
  }

  return {
    status: "success",
    version,
    type: r.type === "image" ? "image" : "video",
    id: r.id || "",
    description: r.desc || "",
    author: {
      username:
        r?.author?.username ||
        r?.author?.nickname ||
        "",
      nickname: r?.author?.nickname || r?.author?.nickname || "",
      avatar: firstUrl(r?.author?.avatarThumb) || r?.author?.avatar || "",
    },
    statistics: r?.statistics,
    cover: firstUrl(r?.cover) || firstUrl(r?.video?.cover),
    duration: r?.video?.duration,
    download,
    raw: r,
  };
}

export interface DownloadTarget {
  url: string;
  filename: string;
  contentType: string;
}

const EXT_BY_TYPE: Record<MediaType, string> = {
  video: "mp4",
  image: "jpg",
  music: "mp3",
};

const CT_BY_TYPE: Record<MediaType, string> = {
  video: "video/mp4",
  image: "image/jpeg",
  music: "audio/mpeg",
};

export async function resolveDownload(
  url: string,
  type: MediaType,
  index = 0,
  version: DownloaderVersion = "v1"
): Promise<DownloadTarget> {
  const post = await fetchPost(url, { version });

  let mediaUrl = "";
  if (type === "video") {
    mediaUrl = post.download.video[0]?.url || "";
  } else if (type === "image") {
    mediaUrl = post.download.images[index] || post.download.images[0] || "";
  } else if (type === "music") {
    mediaUrl = post.download.music[0]?.url || "";
  }

  if (!mediaUrl) {
    throw new Error(
      `No ${type} media available for this post (type=${post.type}, videos=${post.download.video.length}, images=${post.download.images.length}, music=${post.download.music.length})`
    );
  }

  const base = post.author.username || post.id || "tiktok";
  const suffix = type === "image" && post.download.images.length > 1 ? `_${index + 1}` : "";
  const filename = `${base}_${post.id}_${type}${suffix}.${EXT_BY_TYPE[type]}`;

  return { url: mediaUrl, filename, contentType: CT_BY_TYPE[type] };
}

export function isTiktokUrl(url: string): boolean {
  return /tiktok\.com|douyin\.com|vt\.tiktok/.test(url);
}
