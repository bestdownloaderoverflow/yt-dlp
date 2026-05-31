"""Configuration constants and quality format presets."""

from typing import Optional

# -----------------------------------------------------------------------------
# Quality preset format strings
# -----------------------------------------------------------------------------
QUALITY_FORMATS = {
    "1080": (
        "bestvideo[vcodec^=avc1][height<=1080][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]"
        "/bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]"
        "/best[height<=1080][ext=mp4][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=1080][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=1080]"
        "/best"
    ),
    "720": (
        "bestvideo[vcodec^=avc1][height<=720][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]"
        "/bestvideo[vcodec^=avc1][height<=720]+bestaudio[acodec^=mp4a]"
        "/best[height<=720][ext=mp4][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=720][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=720]"
        "/best"
    ),
    "480": (
        "bestvideo[vcodec^=avc1][height<=480][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]"
        "/bestvideo[vcodec^=avc1][height<=480]+bestaudio[acodec^=mp4a]"
        "/best[height<=480][ext=mp4][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=480][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=480]"
        "/best"
    ),
    "360": (
        "bestvideo[vcodec^=avc1][height<=360][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]"
        "/bestvideo[vcodec^=avc1][height<=360]+bestaudio[acodec^=mp4a]"
        "/best[height<=360][ext=mp4][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=360][vcodec^=avc1][acodec^=mp4a]"
        "/best[height<=360]"
        "/best"
    ),
}

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
AUDIO_FORMAT_M4A = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"

# Chunk sizes for streaming
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per range request (mirrors cobalt)
VIDEO_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB for video chunks
STREAM_BUFFER_SIZE = 256 * 1024  # 256 KB per iteration for streaming responses


# -----------------------------------------------------------------------------
# Helper functions untuk estimasi content length (seperti Cobalt)
# -----------------------------------------------------------------------------

def estimate_mp3_size(duration_seconds: Optional[int], bitrate: int = 192) -> int:
    """
    Estimasi ukuran MP3 berdasarkan durasi dan bitrate.

    Formula: (bitrate kbps * 1000 * duration) / 8 bytes
    Tambah 10% untuk metadata dan variance.

    Args:
        duration_seconds: Durasi video dalam detik
        bitrate: Bitrate MP3 dalam kbps (default 192 untuk libmp3lame)

    Returns:
        Estimated size dalam bytes, atau -1 jika duration tidak diketahui
    """
    if not duration_seconds or duration_seconds <= 0:
        return -1

    # MP3 size = (bitrate * 1000 * duration) / 8
    # Tambah 10% untuk safety margin
    estimated = int((bitrate * 1000 * duration_seconds / 8) * 1.1)
    return estimated


def estimate_video_size(filesize: Optional[int], duration: Optional[int]) -> int:
    """
    Estimasi ukuran video setelah processing.

    Untuk video yang sudah ada filesize-nya, gunakan itu.
    Untuk video tanpa filesize, estimasi berdasarkan durasi.
    """
    if filesize and filesize > 0:
        return int(filesize * 1.05)  # Tambah 5% untuk container overhead

    if duration and duration > 0:
        # Estimasi kasar: ~2MB per menit untuk 720p
        return int(duration * 2 * 1024 * 1024 / 60)

    return -1
