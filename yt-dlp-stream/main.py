import io
import logging
import os
import subprocess
import threading
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="yt-dlp Stream API", version="1.0.0")


class PipeWriter(io.RawIOBase):
    def __init__(self, write_fd: int):
        self._fd = write_fd

    def write(self, b: bytes) -> int:
        try:
            os.write(self._fd, b)
        except OSError:
            pass
        return len(b)

    def writable(self) -> bool:
        return True


def _run_ydl(url: str, writer: PipeWriter, write_fd: int, ydl_opts: dict, error_holder: list):
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        error_holder.append(e)
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass


def stream_generator_ffmpeg(
    url: str,
    format_str: str,
    audio_only: bool,
    chunk_size: int = 65536,
):
    """
    Pipeline: yt-dlp (output_stream=PipeWriter) → os.pipe → FFmpeg stdin → FFmpeg stdout → yield chunks
    Yields: (chunk_bytes, filename)
    """
    ydl_read_fd, ydl_write_fd = os.pipe()
    writer = PipeWriter(ydl_write_fd)
    error_holder: list = []

    # Get info for filename without extra request
    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            base_filename = info.get("title", "download") or "download"
            ext = "mp3" if audio_only else "mp4"
            out_filename = f"{base_filename}.{ext}"
    except Exception:
        out_filename = f"download.{'mp3' if audio_only else 'mp4'}"

    # Yield filename first so endpoint can capture it
    yield b"", out_filename

    ydl_opts = {
        "outtmpl": "-",
        "output_stream": writer,
        "quiet": True,
        "no_warnings": True,
        "format": format_str,
    }

    if audio_only:
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-vn",
            "-acodec", "libmp3lame", "-q:a", "2",
            "-f", "mp3",
            "pipe:1",
        ]
    else:
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+faststart",
            "-f", "mp4",
            "pipe:1",
        ]

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=os.fdopen(ydl_read_fd, "rb"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    ydl_thread = threading.Thread(
        target=_run_ydl,
        args=(url, writer, ydl_write_fd, ydl_opts, error_holder),
        daemon=True,
    )
    ydl_thread.start()

    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk, None
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()
        ydl_thread.join()

    if error_holder:
        logger.error("yt-dlp error: %s", error_holder[0])

    if ffmpeg_proc.returncode not in (0, None):
        stderr_out = ffmpeg_proc.stderr.read().decode(errors="replace")
        logger.error("FFmpeg exited %d: %s", ffmpeg_proc.returncode, stderr_out)


@app.get("/")
def root():
    return {"message": "yt-dlp Stream API", "docs": "/docs"}


@app.get("/info")
def get_info(url: str = Query(..., description="Video URL")):
    """Extract video metadata without downloading."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "id": info.get("id"),
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
                "formats": [
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution"),
                        "filesize": f.get("filesize"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                    }
                    for f in info.get("formats", [])
                ],
            }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/video")
def stream_video(
    url: str = Query(..., description="Video URL"),
    format: Optional[str] = Query("best[ext=mp4]/best", description="yt-dlp format string"),
    download: bool = Query(False, description="Download as attachment instead of streaming inline"),
):
    """Stream video via yt-dlp → FFmpeg → client (no disk write)."""
    gen = stream_generator_ffmpeg(url, format_str=format, audio_only=False)

    # Extract filename from first yield
    _, filename = next(gen)

    def chunk_generator():
        for chunk, _ in gen:
            yield chunk

    disp = f'{"attachment" if download else "inline"}; filename="{filename}"'

    return StreamingResponse(
        chunk_generator(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": disp,
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/stream/audio")
def stream_audio(
    url: str = Query(..., description="Video URL"),
    download: bool = Query(False, description="Download as attachment instead of streaming inline"),
):
    """Stream audio (mp3) via yt-dlp → FFmpeg → client (no disk write)."""
    gen = stream_generator_ffmpeg(url, format_str="bestaudio/best", audio_only=True)

    # Extract filename from first yield
    _, filename = next(gen)

    def chunk_generator():
        for chunk, _ in gen:
            yield chunk

    disp = f'{"attachment" if download else "inline"}; filename="{filename}"'

    return StreamingResponse(
        chunk_generator(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": disp,
            "X-Accel-Buffering": "no",
        },
    )
