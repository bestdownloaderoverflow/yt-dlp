"""Slideshow rendering: photo post -> MP4 via ffmpeg."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

import httpx

FFMPEG_TIMEOUT_SECONDS = 90
DEFAULT_DURATION_PER_IMAGE = 4
BUFFER_SIZE = 256 * 1024

CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
}

logger = logging.getLogger("gateway.slideshow")


class SlideshowError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


async def _download_to_file(
    url: str, dest: str, headers: dict, client: httpx.AsyncClient
) -> None:
    resp = await client.get(url, headers=headers, follow_redirects=True)
    if not resp.is_success:
        status = resp.status_code
        raise SlideshowError(
            f"Failed to download media: HTTP {status}",
            404 if 400 <= status < 500 else 502,
        )
    with open(dest, "wb") as f:
        f.write(resp.content)


async def render_slideshow(
    photo_urls: List[str],
    audio_url: Optional[str],
    referer: str = "https://www.tiktok.com/",
    ffmpeg_path: Optional[str] = None,
    duration_per_image: int = DEFAULT_DURATION_PER_IMAGE,
) -> dict:
    if not photo_urls:
        raise SlideshowError("No photos available for slideshow", 400)

    ffmpeg = (
        ffmpeg_path
        or os.environ.get("FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or "ffmpeg"
    )

    temp_dir = tempfile.mkdtemp(prefix="tkdl_slideshow_")
    output_path = os.path.join(temp_dir, "slideshow.mp4")

    headers = {
        "User-Agent": CDN_HEADERS["User-Agent"],
        "Referer": referer,
        "Accept-Encoding": "identity",
    }

    timeout = httpx.Timeout(60, connect=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        image_paths: List[str] = []
        for i, url in enumerate(photo_urls):
            dst = os.path.join(temp_dir, f"image_{i}.jpg")
            await _download_to_file(url, dst, headers, client)
            image_paths.append(dst)

        audio_path: Optional[str] = None
        if audio_url and audio_url.strip():
            audio_path = os.path.join(temp_dir, "audio.mp3")
            try:
                await _download_to_file(audio_url, audio_path, headers, client)
            except Exception as exc:
                logger.warning("audio download failed, rendering without audio: %s", exc)
                audio_path = None

    args: List[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for img in image_paths:
        args.extend(["-loop", "1", "-t", str(duration_per_image), "-i", img])

    audio_input_index = -1
    if audio_path and os.path.exists(audio_path):
        audio_input_index = len(image_paths)
        args.extend(["-stream_loop", "-1", "-i", audio_path])

    filter_parts: List[str] = []
    concat_inputs: List[str] = []
    for i in range(len(image_paths)):
        filter_parts.append(
            f"[{i}:v]scale=w=720:h=1280:force_original_aspect_ratio=decrease,"
            f"pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"fps=24,trim=duration={duration_per_image},setpts=PTS-STARTPTS[v{i}]"
        )
        concat_inputs.append(f"[v{i}]")
    filter_parts.append(
        f"{''.join(concat_inputs)}concat=n={len(image_paths)}:v=1:a=0[vout]"
    )

    if audio_path and os.path.exists(audio_path):
        total_duration = len(image_paths) * duration_per_image
        filter_parts.append(
            f"[{audio_input_index}:a]atrim=0:{total_duration},asetpts=PTS-STARTPTS[aout]"
        )
        args.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
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
                output_path,
            ]
        )
    else:
        args.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
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
                output_path,
            ]
        )

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()
        cleanup_temp(temp_dir)
        raise SlideshowError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s", 504)

    if proc.returncode != 0:
        err = (
            stderr_data.decode("utf-8", errors="replace")[-400:]
            if stderr_data
            else "unknown"
        )
        cleanup_temp(temp_dir)
        raise SlideshowError(f"ffmpeg failed (exit {proc.returncode}): {err}", 502)

    file_size = os.path.getsize(output_path)
    logger.info("slideshow rendered: %d bytes, %d images", file_size, len(image_paths))
    return {"output_path": output_path, "temp_dir": temp_dir, "file_size": file_size}


def cleanup_temp(temp_dir: str) -> None:
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as exc:
        logger.warning("failed to cleanup temp dir %s: %s", temp_dir, exc)


def open_file_stream(path: str):
    return open(path, "rb")
