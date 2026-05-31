"""
Utility functions untuk yt-dlp-stream.
"""
from typing import Optional


# Services yang butuh chunked download (seperti Cobalt)
CHUNKED_SERVICES = {"youtube", "tiktok", "vk"}


def detect_service(url: str) -> str:
    """Detect service dari URL."""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    elif "tiktok.com" in url_lower:
        return "tiktok"
    elif "vk.com" in url_lower:
        return "vk"
    elif "instagram.com" in url_lower:
        return "instagram"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    return "generic"


def should_use_chunked(url: str, duration: Optional[int] = None) -> bool:
    """
    Determine if URL should use chunked download.

    Cobalt hanya pakai chunked untuk YouTube dan VK.
    Untuk service lain, pakai direct proxy.
    """
    service = detect_service(url)

    # Always chunk untuk YouTube dan VK
    if service in {"youtube", "vk"}:
        return True

    # Chunk untuk video panjang (> 10 menit) dari service lain
    if duration and duration > 600:
        return True

    return False
