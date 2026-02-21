"""Info endpoint for video metadata extraction."""

from typing import Optional

import yt_dlp
from fastapi import APIRouter, HTTPException, Query

from core.helpers import _enforce_rate_limit
from ytdl_manager import ydl_manager

router = APIRouter()


@router.get("/info")
def get_info(
    url: str = Query(..., description="Video URL"),
    proxy: Optional[str] = Query(None, description="Proxy URL (e.g., http://127.0.0.1:8080)"),
    impersonate: Optional[str] = Query(None, description="Browser to impersonate for TLS fingerprinting (e.g., chrome, safari)"),
):
    """Extract video metadata without downloading."""
    try:
        # Gunakan singleton manager untuk session integrity
        info = ydl_manager.extract_info(url, proxy=proxy, impersonate=impersonate)
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
                    "height": f.get("height"),
                    "width": f.get("width"),
                    "filesize": f.get("filesize"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "tbr": f.get("tbr"),
                    "protocol": f.get("protocol"),
                }
                for f in info.get("formats", [])
            ],
        }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
