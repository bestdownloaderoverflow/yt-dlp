"""FFmpeg command builders for video/audio processing."""

from core.helpers import _header_str, _is_hls, needs_ios_video_transcode


def _ios_video_codec_args(fmt: dict) -> list[str]:
    """
    Prefer stream copy, but transcode non-iOS-friendly codecs (VP9/AV1/etc) to H.264.
    """
    if needs_ios_video_transcode(fmt):
        return [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.1",
        ]
    return ["-c:v", "copy"]


def build_ffmpeg_merge_cmd(
    video_fmt: dict,
    audio_fmt: dict,
    global_headers: dict,
) -> list[str]:
    """
    Build an ffmpeg command that reads a separate video stream and audio stream
    and muxes them into fragmented MP4 on stdout.
    Handles plain HTTP, HLS (.m3u8) and DASH (.mpd) manifests transparently.

    Per-format http_headers from yt-dlp (already computed by _calc_headers)
    take priority over global_headers.

    For HLS: ffmpeg applies -headers to ALL sub-requests (manifest, segments,
    key URIs) automatically, matching yt-dlp's FFmpegFD behaviour.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    for fmt in (video_fmt, audio_fmt):
        headers = fmt.get("http_headers") or global_headers
        hdr = _header_str(headers)
        if hdr:
            cmd.extend(["-headers", hdr])
        cmd.extend(["-i", fmt["url"]])

    # -c:a aac re-encodes audio; needed when merging HLS/DASH streams where
    # audio may be in ADTS/raw AAC that cannot be directly muxed into MP4.
    # Mirror of yt-dlp FFmpegFD which also uses -c:a aac for merge cases.
    cmd.extend([
        "-map", "0:v:0",
        "-map", "1:a:0",
    ])
    cmd.extend(_ios_video_codec_args(video_fmt))
    cmd.extend([
        "-c:a", "aac",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ])
    return cmd


def build_ffmpeg_single_cmd(
    fmt: dict,
    global_headers: dict,
    audio_only: bool,
) -> list[str]:
    """
    Build an ffmpeg command for a single-stream input (muxed video+audio,
    HLS/DASH manifest, or audio-only).  Encodes to MP3 when audio_only=True,
    otherwise remuxes to fragmented MP4.

    Per-format http_headers from yt-dlp (already computed by _calc_headers)
    take priority over global_headers.

    For HLS/DASH: ffmpeg applies -headers to ALL sub-requests automatically
    (manifest fetch, every segment, AES-128 key URI), matching yt-dlp's
    FFmpegFD behaviour where headers are set once per input.

    For HLS→MP4: adds -bsf:a aac_adtstoasc to fix AAC ADTS→MP4 muxing,
    mirroring yt-dlp FFmpegFD (external.py line ~609).
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    headers = fmt.get("http_headers") or global_headers
    hdr = _header_str(headers)
    if hdr:
        cmd.extend(["-headers", hdr])

    cmd.extend(["-i", fmt["url"]])

    if audio_only:
        cmd.extend([
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            "-f", "mp3",
            "pipe:1",
        ])
    else:
        if needs_ios_video_transcode(fmt):
            out_args = [
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-profile:v", "high",
                "-level", "4.1",
                "-c:a", "aac",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "-f", "mp4",
            ]
        else:
            out_args = [
                "-c", "copy",
                "-movflags", "frag_keyframe+empty_moov+faststart",
                "-f", "mp4",
            ]
            # HLS streams with AAC audio need ADTS-to-ASC bitstream filter
            # when muxing into MP4, otherwise audio is unplayable.
            # Mirrors yt-dlp FFmpegFD behaviour (downloader/external.py).
            acodec = fmt.get("acodec", "") or ""
            if _is_hls(fmt) and acodec.split(".")[0] in ("aac", "mp4a", ""):
                out_args = ["-c", "copy", "-bsf:a", "aac_adtstoasc",
                            "-movflags", "frag_keyframe+empty_moov+faststart",
                            "-f", "mp4"]
        cmd.extend(out_args)
        cmd.append("pipe:1")

    return cmd
