#!/usr/bin/env python3
"""
Stream MP3 from a cached download session via ffmpeg to stdout.
Uses the pre-resolved direct URL (no re-extraction).
"""

from __future__ import annotations

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.redis_cache import download_cache  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: mp3_stream.py <download-key>", file=sys.stderr)
        return 2

    key = sys.argv[1].strip()
    session = download_cache.get_session(key)
    if not session or session.type != "mp3":
        print("invalid mp3 download session", file=sys.stderr)
        return 3

    direct_url = getattr(session, "direct_url", None)
    if not direct_url:
        print("no direct_url in session", file=sys.stderr)
        return 3

    # Build ffmpeg command: read from URL, transcode to MP3, output to stdout
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", direct_url,
        "-vn",
        "-acodec", "libmp3lame", "-b:a", "192k",
        "-f", "mp3", "pipe:1",
    ]

    # Pass HTTP headers if available
    http_headers = getattr(session, "http_headers", None) or {}
    cookies = getattr(session, "cookies", None)
    if http_headers or cookies:
        headers_str = ""
        for k, v in http_headers.items():
            kl = k.lower()
            if kl in ("accept-encoding", "cookie"):
                continue
            headers_str += f"{k}: {v}\r\n"
        if cookies:
            headers_str += f"Cookie: {cookies}\r\n"
        if headers_str:
            # Insert headers before -i
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-headers", headers_str,
                "-i", direct_url,
                "-vn",
                "-acodec", "libmp3lame", "-b:a", "192k",
                "-f", "mp3", "pipe:1",
            ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except BrokenPipeError:
        proc.kill()
        return 0
    finally:
        proc.wait()

    if proc.returncode != 0:
        err = proc.stderr.read().decode(errors="replace").strip()
        print(f"ffmpeg error: {err}", file=sys.stderr)
        return proc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
