"""Streaming generators for video/audio content."""

import asyncio
import logging
import os
import queue
import re
import subprocess
import threading
from typing import Optional, AsyncIterator

import httpx
from fastapi import HTTPException, Request as FastAPIRequest

from core.config import CHUNK_SIZE, VIDEO_CHUNK_SIZE, AUDIO_FORMAT
from core.ffmpeg import build_ffmpeg_merge_cmd, build_ffmpeg_single_cmd
from core.helpers import _extract_info_and_resolve, needs_ios_video_transcode
from process_manager import process_manager
from ytdl_manager import ydl_manager

logger = logging.getLogger("ytdl_stream")


_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


def _parse_content_range(content_range: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    if not content_range:
        return None, None, None
    match = _CONTENT_RANGE_RE.match(content_range.strip())
    if not match:
        return None, None, None
    start = int(match.group(1))
    end = int(match.group(2))
    total_raw = match.group(3)
    total = None if total_raw == "*" else int(total_raw)
    return start, end, total


def stream_generator_ffmpeg(
    url: str,
    format_str: str,
    audio_only: bool,
    ydl_opts: dict,
    chunk_size: int = 65536,
):
    """
    Pipeline:
    1. yt-dlp extracts video info (with full http_headers computed per-format
       via _calc_headers, including User-Agent, Referer, etc.)
    2. Detect strategy:
       - audio_only          → single stream, encode to MP3
       - 2 resolved formats  → video-only + audio-only, merge via ffmpeg
       - 1 resolved format   → single muxed stream or HLS/DASH manifest
    3. ffmpeg downloads / demuxes / remuxes and pipes to stdout
    4. Yield (b"", filename) first, then (chunk, None) for each data chunk

    NOTE: Sekarang menggunakan singleton ydl_manager untuk session integrity.
    """
    # Gunakan singleton manager untuk session integrity
    info = ydl_manager.extract_info(
        url,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

    if not info:
        raise ValueError("Could not extract video info")

    base_filename = info.get("title", "download") or "download"
    ext = "mp3" if audio_only else "mp4"
    out_filename = f"{base_filename}.{ext}"

    yield b"", out_filename

    # global_headers: fallback if a format has no per-format http_headers.
    # yt-dlp already strips Cookie from here (security) and injects
    # per-format headers (User-Agent, Referer, etc.) via _calc_headers.
    global_headers = info.get("http_headers") or {}
    resolved = ydl_manager.resolve_formats(
        info, format_str,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

    if audio_only:
        ffmpeg_cmd = build_ffmpeg_single_cmd(
            fmt=resolved[0],
            global_headers=global_headers,
            audio_only=True,
        )
    elif len(resolved) == 2:
        video_fmt, audio_fmt = resolved[0], resolved[1]
        if video_fmt.get("vcodec", "none") in (None, "none", ""):
            video_fmt, audio_fmt = audio_fmt, video_fmt
        ffmpeg_cmd = build_ffmpeg_merge_cmd(
            video_fmt=video_fmt,
            audio_fmt=audio_fmt,
            global_headers=global_headers,
        )
    else:
        ffmpeg_cmd = build_ffmpeg_single_cmd(
            fmt=resolved[0],
            global_headers=global_headers,
            audio_only=False,
        )

    logger.debug(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    # Register process untuk automatic cleanup
    process_manager.register_process(ffmpeg_proc, process_type="ffmpeg_stream")

    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk, None
    finally:
        ffmpeg_proc.stdout.close()
        ffmpeg_proc.wait()
        process_manager.unregister_process(ffmpeg_proc.pid)


def stream_generator_direct(
    url: str,
    format_str: str,
    ydl_opts: dict,
):
    """
    Resolve the best audio URL via yt-dlp, then yield:
      (b"", filename, http_headers, filesize, direct_url)
    for the caller to perform a chunked range download without ffmpeg.

    NOTE: Sekarang menggunakan singleton ydl_manager untuk session integrity.
    """
    # Gunakan singleton manager untuk session integrity
    info = ydl_manager.extract_info(
        url,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

    if not info:
        raise ValueError("Could not extract video info")

    base_filename = info.get("title", "download") or "download"
    global_headers = info.get("http_headers") or {}
    resolved = ydl_manager.resolve_formats(
        info, format_str,
        proxy=ydl_opts.get("proxy"),
        impersonate=ydl_opts.get("impersonate")
    )

    fmt = resolved[0]
    ext = fmt.get("ext") or "m4a"
    out_filename = f"{base_filename}.{ext}"
    direct_url = fmt["url"]
    http_headers = fmt.get("http_headers") or global_headers
    filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

    return out_filename, direct_url, http_headers, filesize, ext


async def _chunked_range_generator(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """
    Cobalt-style chunked range download: fetches the remote file in
    sequential Range requests so each chunk is independently retryable
    and the connection never times out on large files.
    """
    read = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        while read < total_size:
            end = min(read + chunk_size - 1, total_size - 1)
            range_headers = {**headers, "Range": f"bytes={read}-{end}"}
            async with client.stream("GET", url, headers=range_headers) as resp:
                resp.raise_for_status()
                async for data in resp.aiter_bytes():
                    yield data
                    read += len(data)


async def _chunked_video_generator(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int,
    ydl_refresh_info: dict,  # For URL transplanting
    cancel_event: threading.Event = None,  # For client disconnect cancellation
) -> AsyncIterator[bytes]:
    """
    Cobalt-style chunked range download for video.
    Fetches the remote file in sequential Range requests so each chunk
    is independently retryable and the connection never times out on large files.
    Supports URL transplanting when URLs expire (403 Forbidden).

    Improvements:
    - Per-chunk retry dengan exponential backoff
    - Better error propagation ke client
    - Detailed logging untuk debugging
    - Client disconnect detection via cancel_event
    """
    read = 0
    refresh_count = 0
    max_refreshes = 10
    max_retries_per_chunk = 3

    safe_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        while read < total_size:
            # Check for cancellation
            if cancel_event and cancel_event.is_set():
                logger.info(f"Chunked download cancelled at byte {read}/{total_size}")
                break

            end = min(read + chunk_size - 1, total_size - 1)
            range_headers = {**safe_headers, "Range": f"bytes={read}-{end}"}

            chunk_retries = 0
            chunk_success = False

            while chunk_retries < max_retries_per_chunk and not chunk_success:
                # Check for cancellation before retry
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Chunked download cancelled at byte {read}/{total_size}")
                    break

                try:
                    async with client.stream("GET", url, headers=range_headers) as resp:
                        # 416: requested range cannot be satisfied. If we're already at EOF,
                        # treat this as completion to match resilient downloader behaviour.
                        if resp.status_code == 416:
                            if read >= total_size:
                                chunk_success = True
                                break
                            raise HTTPException(
                                status_code=416,
                                detail=f"Requested range not satisfiable at {read}/{total_size} bytes",
                            )

                        # Handle 403 - URL expired, try transplant
                        if resp.status_code == 403:
                            if refresh_count < max_refreshes:
                                logger.warning(f"URL expired at byte {read}/{total_size}, attempting refresh {refresh_count+1}/{max_refreshes}...")
                                new_url = await _refresh_url(ydl_refresh_info)
                                if new_url:
                                    url = new_url
                                    refresh_count += 1
                                    logger.info(f"URL refreshed successfully (attempt {refresh_count})")
                                    continue  # Retry same chunk with new URL
                                else:
                                    logger.error("Failed to refresh URL")
                                    raise HTTPException(
                                        status_code=403,
                                        detail=f"URL expired at {read}/{total_size} bytes and could not be refreshed"
                                    )
                            else:
                                raise HTTPException(
                                    status_code=403,
                                    detail=f"Max refresh attempts ({max_refreshes}) exceeded at {read}/{total_size} bytes"
                                )

                        # Handle other errors
                        if resp.status_code >= 400:
                            logger.error(f"HTTP {resp.status_code} at byte {read}/{total_size}")
                            resp.raise_for_status()

                        # Range semantics validation (yt-dlp HttpFD style safety):
                        # - 206 should match requested range start
                        # - 200 is acceptable only on first chunk (server ignored Range)
                        if resp.status_code == 206:
                            cr_start, _, cr_total = _parse_content_range(resp.headers.get("Content-Range"))
                            if cr_start is None or cr_start != read:
                                raise HTTPException(
                                    status_code=502,
                                    detail=(
                                        f"Invalid Content-Range response at byte {read}: "
                                        f"{resp.headers.get('Content-Range')}"
                                    ),
                                )
                            if cr_total is not None and cr_total != total_size:
                                logger.warning(
                                    "Content-Range total (%s) differs from expected total_size (%s)",
                                    cr_total, total_size,
                                )
                        elif resp.status_code == 200 and read > 0:
                            raise HTTPException(
                                status_code=502,
                                detail=(
                                    "Server ignored Range request mid-download; "
                                    "cannot continue safely without duplicate bytes"
                                ),
                            )

                        # Success - stream chunk data
                        chunk_bytes_read = 0
                        async for data in resp.aiter_bytes():
                            # Check for cancellation during streaming
                            if cancel_event and cancel_event.is_set():
                                logger.info(f"Chunked download cancelled during streaming at byte {read}/{total_size}")
                                break
                            yield data
                            read += len(data)
                            chunk_bytes_read += len(data)

                        # If cancelled during streaming, exit outer loop
                        if cancel_event and cancel_event.is_set():
                            break

                        chunk_success = True

                        # Log progress setiap 10%
                        progress = (read / total_size) * 100
                        if int(progress) % 10 == 0:
                            logger.info(f"Download progress: {progress:.1f}% ({read}/{total_size} bytes)")

                except httpx.HTTPStatusError as e:
                    chunk_retries += 1

                    # Special handling untuk 403
                    if e.response.status_code == 403 and refresh_count < max_refreshes:
                        logger.warning(f"HTTP 403 at byte {read}, attempting refresh...")
                        new_url = await _refresh_url(ydl_refresh_info)
                        if new_url:
                            url = new_url
                            refresh_count += 1
                            continue

                    # Retry lain dengan backoff
                    if chunk_retries < max_retries_per_chunk:
                        backoff = min(2 ** chunk_retries, 10)  # Max 10s
                        logger.warning(f"Chunk failed (retry {chunk_retries}/{max_retries_per_chunk}), retrying in {backoff}s: {e}")
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"Chunk failed after {max_retries_per_chunk} retries at byte {read}/{total_size}: {e}")
                        raise HTTPException(
                            status_code=e.response.status_code,
                            detail=f"Download failed at {read}/{total_size} bytes after {max_retries_per_chunk} retries: {str(e)}"
                        )

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    chunk_retries += 1
                    if chunk_retries < max_retries_per_chunk:
                        backoff = min(2 ** chunk_retries, 10)
                        logger.warning(f"Network error (retry {chunk_retries}/{max_retries_per_chunk}), retrying in {backoff}s: {e}")
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"Network error after {max_retries_per_chunk} retries at byte {read}/{total_size}: {e}")
                        raise HTTPException(
                            status_code=503,
                            detail=f"Network error at {read}/{total_size} bytes after {max_retries_per_chunk} retries: {str(e)}"
                        )

                except HTTPException:
                    raise

                except Exception as e:
                    logger.error(f"Unexpected error at byte {read}/{total_size}: {e}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Unexpected error at {read}/{total_size} bytes: {str(e)}"
                    )

            if not chunk_success:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to download chunk at {read}/{total_size} bytes after all retries"
                )


async def _chunked_video_generator_with_disconnect(
    url: str,
    headers: dict,
    total_size: int,
    chunk_size: int,
    ydl_refresh_info: dict,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Wrapper untuk _chunked_video_generator yang menambahkan client disconnect detection.
    Menggunakan threading.Event untuk signal cancellation ke generator.
    """
    cancel_event = threading.Event()

    async def disconnect_checker():
        """Task untuk monitor client disconnect."""
        while not cancel_event.is_set():
            if await request.is_disconnected():
                cancel_event.set()
                logger.info("Client disconnect detected, signalling cancellation")
                break
            await asyncio.sleep(0.5)  # Check every 500ms

    # Start disconnect checker task
    checker_task = asyncio.create_task(disconnect_checker())

    try:
        async for chunk in _chunked_video_generator(
            url, headers, total_size, chunk_size, ydl_refresh_info, cancel_event
        ):
            yield chunk
    finally:
        # Ensure checker task is cancelled
        cancel_event.set()
        checker_task.cancel()
        try:
            await checker_task
        except asyncio.CancelledError:
            pass


async def _stream_chunked_merge(
    video_fmt: dict,
    audio_fmt: dict,
    global_headers: dict,
    ydl_opts: dict,
    original_url: str,
    format_str: str,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Stream video+audio merge dengan true streaming menggunakan named pipes.

    Video dan audio di-download secara parallel ke named pipes,
    ffmpeg membaca dari pipes sementara download berlangsung.
    Client dapat menerima data MP4 hampir seketika.
    """
    video_headers = video_fmt.get("http_headers") or global_headers
    audio_headers = audio_fmt.get("http_headers") or global_headers
    video_url = video_fmt["url"]
    audio_url = audio_fmt["url"]
    video_size = video_fmt.get("filesize") or video_fmt.get("filesize_approx") or 0
    audio_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0

    ydl_refresh_info_video = {
        "url": original_url,
        "format_str": format_str,
        "ydl_opts": ydl_opts,
        "target": "video",
        "format_id": video_fmt.get("format_id"),
        "format_idx": 0,
    }
    ydl_refresh_info_audio = {
        "url": original_url,
        "format_str": format_str,
        "ydl_opts": ydl_opts,
        "target": "audio",
        "format_id": audio_fmt.get("format_id"),
        "format_idx": 1,
    }

    # Cancellation event untuk signal disconnect
    cancel_event = threading.Event()

    # Queues untuk video dan audio streams
    video_queue = queue.Queue(maxsize=32)
    audio_queue = queue.Queue(maxsize=32)
    video_done = threading.Event()
    audio_done = threading.Event()

    def download_video_to_queue():
        """Download video dalam thread terpisah."""
        try:
            if cancel_event.is_set():
                return
            if video_size:
                asyncio.run(_download_chunked_to_queue(
                    video_url, video_headers, video_size,
                    VIDEO_CHUNK_SIZE, ydl_refresh_info_video, video_queue, cancel_event
                ))
            else:
                asyncio.run(_download_simple_to_queue(
                    video_url, video_headers, video_queue, cancel_event
                ))
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Video download error: {e}")
                video_queue.put(("ERROR", str(e)))
        finally:
            video_done.set()
            video_queue.put(None)

    def download_audio_to_queue():
        """Download audio dalam thread terpisah."""
        try:
            if cancel_event.is_set():
                return
            if audio_size:
                asyncio.run(_download_chunked_to_queue(
                    audio_url, audio_headers, audio_size,
                    VIDEO_CHUNK_SIZE, ydl_refresh_info_audio, audio_queue, cancel_event
                ))
            else:
                asyncio.run(_download_simple_to_queue(
                    audio_url, audio_headers, audio_queue, cancel_event
                ))
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Audio download error: {e}")
                audio_queue.put(("ERROR", str(e)))
        finally:
            audio_done.set()
            audio_queue.put(None)

    # Start download threads
    video_thread = threading.Thread(target=download_video_to_queue, daemon=True)
    audio_thread = threading.Thread(target=download_audio_to_queue, daemon=True)
    video_thread.start()
    audio_thread.start()

    # Create pipe for audio input and use the actual FD number in ffmpeg args.
    # Hardcoding pipe:3 can fail because os.pipe() does not guarantee fd=3.
    audio_r, audio_w = os.pipe()
    audio_pipe_input = f"pipe:{audio_r}"

    # Start ffmpeg dengan pipe untuk kedua input.
    # For iOS playback compatibility, transcode non-AVC video tracks to H.264.
    video_codec_args = ["-c:v", "copy"]
    if needs_ios_video_transcode(video_fmt):
        logger.info(
            "Chunked merge: video codec '%s' is not iOS-friendly, transcoding to H.264",
            video_fmt.get("vcodec"),
        )
        video_codec_args = [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.1",
        ]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",  # Video from stdin (we'll use a thread to feed it)
        "-i", audio_pipe_input,  # Audio from dedicated pipe fd
        "-map", "0:v:0",
        "-map", "1:a:0",
        *video_codec_args,
        "-c:a", "aac",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ]

    ffmpeg_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=[audio_r],
    )

    # Register process untuk automatic cleanup
    process_manager.register_process(ffmpeg_proc, process_type="ffmpeg_merge")

    # Close read end in parent
    os.close(audio_r)

    # Threads untuk feed ffmpeg inputs
    def feed_video():
        try:
            while not cancel_event.is_set():
                chunk = video_queue.get()
                if chunk is None:
                    break
                # Check for error sentinel
                if isinstance(chunk, tuple) and chunk[0] == "ERROR":
                    logger.error(f"Video download failed: {chunk[1]}")
                    ffmpeg_proc.terminate()
                    break
                ffmpeg_proc.stdin.write(chunk)
            ffmpeg_proc.stdin.close()
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Video feed error: {e}")
            try:
                ffmpeg_proc.terminate()
            except:
                pass

    def feed_audio():
        try:
            while not cancel_event.is_set():
                chunk = audio_queue.get()
                if chunk is None:
                    break
                # Check for error sentinel
                if isinstance(chunk, tuple) and chunk[0] == "ERROR":
                    logger.error(f"Audio download failed: {chunk[1]}")
                    try:
                        ffmpeg_proc.terminate()
                    except:
                        pass
                    break
                os.write(audio_w, chunk)
            os.close(audio_w)
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Audio feed error: {e}")
            try:
                ffmpeg_proc.terminate()
            except:
                pass

    threading.Thread(target=feed_video, daemon=True).start()
    threading.Thread(target=feed_audio, daemon=True).start()

    # Async generator: baca dari ffmpeg stdout dengan disconnect detection
    try:
        loop = asyncio.get_event_loop()
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping chunked merge stream")
                cancel_event.set()
                break

            chunk = await loop.run_in_executor(
                None, ffmpeg_proc.stdout.read, 65536
            )
            if not chunk:
                break
            yield chunk
    finally:
        # Signal cancellation ke semua threads
        cancel_event.set()

        # Cleanup ffmpeg
        try:
            ffmpeg_proc.stdout.close()
            ffmpeg_proc.terminate()
            ffmpeg_proc.wait(timeout=2)
        except:
            try:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait(timeout=1)
            except:
                pass
        process_manager.unregister_process(ffmpeg_proc.pid)

        # Cleanup audio pipe
        try:
            os.close(audio_w)
        except:
            pass

        # Drain queues untuk unblock threads
        try:
            while not video_queue.empty():
                video_queue.get_nowait()
        except:
            pass
        try:
            while not audio_queue.empty():
                audio_queue.get_nowait()
        except:
            pass


async def _stream_mp3_chunked(
    audio_fmt: dict,
    global_headers: dict,
    ydl_opts: dict,
    original_url: str,
    request: FastAPIRequest,
) -> AsyncIterator[bytes]:
    """
    Stream MP3 dengan true streaming menggunakan pipe.

    FFmpeg membaca dari stdin sementara download berlangsung,
    jadi client dapat menerima data MP3 hampir seketika.

    Key: Gunakan thread untuk download dan pipe ke ffmpeg stdin,
    sementara asyncio membaca stdout ffmpeg dan yield ke client.
    """
    audio_headers = audio_fmt.get("http_headers") or global_headers
    audio_url = audio_fmt["url"]
    audio_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0

    ydl_refresh_info = {
        "url": original_url,
        "format_str": AUDIO_FORMAT,
        "ydl_opts": ydl_opts,
        "target": "single",
        "format_id": audio_fmt.get("format_id"),
        "format_idx": 0,
    }

    # Cancellation event untuk signal disconnect
    cancel_event = threading.Event()

    # Queue untuk komunikasi antara download thread dan async generator
    download_queue = queue.Queue(maxsize=32)  # Buffer ~2MB
    download_done = threading.Event()
    download_error = [None]

    def download_to_queue():
        """Download audio dalam thread terpisah, push ke queue."""
        try:
            if cancel_event.is_set():
                return
            if audio_size:
                # Use chunked download
                asyncio.run(_download_chunked_to_queue(
                    audio_url, audio_headers, audio_size,
                    CHUNK_SIZE, ydl_refresh_info, download_queue, cancel_event
                ))
            else:
                # Simple download
                asyncio.run(_download_simple_to_queue(
                    audio_url, audio_headers, download_queue, cancel_event
                ))
        except Exception as e:
            if not cancel_event.is_set():
                download_error[0] = e
                logger.error(f"Download error: {e}")
        finally:
            download_done.set()
            download_queue.put(None)  # Signal EOF

    # Start download thread
    download_thread = threading.Thread(target=download_to_queue, daemon=True)
    download_thread.start()

    # Start ffmpeg dengan pipe stdin
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",  # Read from stdin
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        "-f", "mp3",
        "pipe:1",
    ]

    ffmpeg_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    # Register process untuk automatic cleanup
    process_manager.register_process(ffmpeg_proc, process_type="ffmpeg_audio")

    # Thread untuk feed ffmpeg stdin dari queue
    def feed_ffmpeg_stdin():
        try:
            while not cancel_event.is_set():
                chunk = download_queue.get()
                if chunk is None:
                    break
                ffmpeg_proc.stdin.write(chunk)
            ffmpeg_proc.stdin.close()
        except Exception as e:
            if not cancel_event.is_set():
                logger.error(f"Feed error: {e}")

    feed_thread = threading.Thread(target=feed_ffmpeg_stdin, daemon=True)
    feed_thread.start()

    # Async generator: baca dari ffmpeg stdout dan yield ke client dengan disconnect detection
    try:
        loop = asyncio.get_event_loop()
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping MP3 stream")
                cancel_event.set()
                break

            # Read dari stdout dalam executor agar tidak blocking
            chunk = await loop.run_in_executor(
                None, ffmpeg_proc.stdout.read, 65536
            )
            if not chunk:
                break
            yield chunk
    finally:
        # Signal cancellation ke semua threads
        cancel_event.set()

        # Cleanup ffmpeg
        try:
            ffmpeg_proc.stdout.close()
            ffmpeg_proc.terminate()
            ffmpeg_proc.wait(timeout=2)
        except:
            try:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait(timeout=1)
            except:
                pass
        process_manager.unregister_process(ffmpeg_proc.pid)

        # Drain queue untuk unblock threads
        try:
            while not download_queue.empty():
                download_queue.get_nowait()
        except:
            pass


async def _refresh_url(ydl_refresh_info: dict) -> Optional[str]:
    """
    Refresh expired URL by re-extracting info from yt-dlp.
    This is the 'transplant' mechanism from Cobalt.

    NOTE: Sekarang menggunakan singleton ydl_manager untuk session integrity.
    """
    try:
        loop = asyncio.get_event_loop()

        def _extract():
            # Gunakan singleton manager untuk session integrity
            info = ydl_manager.extract_info(
                ydl_refresh_info["url"],
                proxy=ydl_refresh_info["ydl_opts"].get("proxy"),
                impersonate=ydl_refresh_info["ydl_opts"].get("impersonate")
            )
            if not info:
                return None
            resolved = ydl_manager.resolve_formats(
                info, ydl_refresh_info["format_str"],
                proxy=ydl_refresh_info["ydl_opts"].get("proxy"),
                impersonate=ydl_refresh_info["ydl_opts"].get("impersonate")
            )
            if not resolved:
                return None

            target = ydl_refresh_info.get("target", "single")
            target_format_id = ydl_refresh_info.get("format_id")
            target_idx = ydl_refresh_info.get("format_idx")

            # Prefer exact format_id match if available.
            if target_format_id:
                matched = next(
                    (f for f in resolved if f.get("format_id") == target_format_id),
                    None,
                )
                if matched and matched.get("url"):
                    return matched["url"]

            # Fallback by stream role.
            if target == "video":
                matched = next(
                    (f for f in resolved if (f.get("vcodec") or "none") not in (None, "", "none")),
                    None,
                )
                if matched and matched.get("url"):
                    return matched["url"]
            elif target == "audio":
                matched = next(
                    (
                        f for f in resolved
                        if (f.get("acodec") or "none") not in (None, "", "none")
                        and (f.get("vcodec") or "none") in (None, "", "none")
                    ),
                    None,
                )
                if matched and matched.get("url"):
                    return matched["url"]

            # Fallback by original index (if still valid), else first resolved format.
            if isinstance(target_idx, int) and 0 <= target_idx < len(resolved):
                if resolved[target_idx].get("url"):
                    return resolved[target_idx]["url"]
            return resolved[0].get("url")

        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        logger.error(f"URL refresh failed: {e}")
        return None


async def _download_chunked_to_queue(url, headers, size, chunk_size, refresh_info, q, cancel_event=None):
    """Helper: download chunked dan push ke queue."""
    ydl_refresh_info = refresh_info
    try:
        async for chunk in _chunked_video_generator(
            url, headers, size, chunk_size, ydl_refresh_info, cancel_event
        ):
            if cancel_event and cancel_event.is_set():
                break
            q.put(chunk)
    except Exception as e:
        if cancel_event is None or not cancel_event.is_set():
            logger.error(f"Chunked download error: {e}")
        raise


async def _download_simple_to_queue(url, headers, q, cancel_event=None):
    """Helper: simple download dan push ke queue."""
    safe_headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url, headers=safe_headers) as resp:
            resp.raise_for_status()
            async for data in resp.aiter_bytes():
                if cancel_event and cancel_event.is_set():
                    break
                q.put(data)


def slideshow_generator(
    image_urls: list[str],
    audio_url: Optional[str],
    temp_dir: str,
    duration_per_image: int = 4,
) -> tuple[str, str]:
    """
    Create a slideshow video from images and optional audio using FFmpeg.

    Args:
        image_urls: List of image URLs
        audio_url: Optional audio URL
        temp_dir: Temporary directory for downloaded files
        duration_per_image: Duration per image in seconds (default: 4)

    Returns:
        Tuple of (output_path, filename)
    """
    from pathlib import Path
    import subprocess
    import tempfile

    work_dir = Path(temp_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Download images
    image_paths = []
    for i, img_url in enumerate(image_urls):
        img_path = work_dir / f"image_{i}.jpg"
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            with client.stream("GET", img_url) as response:
                response.raise_for_status()
                with open(img_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
        image_paths.append(str(img_path))

    if not image_paths:
        raise ValueError("No images downloaded")

    output_path = work_dir / "slideshow.mp4"

    # Build FFmpeg command
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

    # Add each image as input with duration
    for img_path in image_paths:
        cmd.extend(["-loop", "1", "-t", str(duration_per_image), "-i", img_path])

    audio_path = None
    if audio_url:
        # Download audio
        audio_path = work_dir / "audio.mp3"
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            with client.stream("GET", audio_url) as response:
                response.raise_for_status()
                with open(audio_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
        # Add audio input with loop
        cmd.extend(["-stream_loop", "-1", "-i", str(audio_path)])

    # Build filter complex
    filter_parts = []

    # Scale and pad each image to 1080x1920 (portrait)
    for i in range(len(image_paths)):
        filter_parts.append(
            f"[{i}:v]scale=w=1080:h=1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[v{i}]"
        )

    # Concatenate all video streams
    concat_inputs = "".join(f"[v{i}]" for i in range(len(image_paths)))
    filter_parts.append(f"{concat_inputs}concat=n={len(image_paths)}:v=1:a=0[vout]")

    if audio_url and audio_path:
        # Calculate total video duration and trim audio
        video_duration = len(image_paths) * duration_per_image
        filter_parts.append(f"[{len(image_paths)}:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS[aout]")

        # Add filter complex and map both video and audio
        filter_complex = ";".join(filter_parts)
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_path)
        ])
    else:
        # No audio - just video
        filter_complex = ";".join(filter_parts)
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            str(output_path)
        ])

    logger.info(f"Creating slideshow with {len(image_paths)} images")

    # Run FFmpeg
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300  # 5 minutes timeout
    )

    if result.returncode != 0:
        logger.error(f"FFmpeg error: {result.stderr}")
        raise Exception(f"FFmpeg failed: {result.stderr}")

    if not output_path.exists():
        raise Exception("Output file was not created")

    logger.info(f"Slideshow created: {output_path}")
    return str(output_path), "slideshow.mp4"


async def slideshow_stream_generator(
    image_urls: list[str],
    audio_url: Optional[str],
    temp_dir: str,
    filename: str,
    chunk_size: int = 65536,
):
    """
    Async generator that creates slideshow and yields chunks for streaming.
    Cleans up temp files after streaming is complete.
    """
    from pathlib import Path

    output_path = None
    try:
        # Run slideshow generator in thread
        loop = asyncio.get_event_loop()
        output_path, _ = await loop.run_in_executor(
            None,
            slideshow_generator,
            image_urls,
            audio_url,
            temp_dir,
        )

        # Stream the file
        with open(output_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    finally:
        # Cleanup temp files
        if output_path and Path(output_path).exists():
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"Cleaned up slideshow temp dir: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to cleanup slideshow temp dir: {e}")
