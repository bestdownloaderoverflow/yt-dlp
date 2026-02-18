import os
import uuid
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

import yt_dlp

from app.config import YDL_BASE_OPTS, TEMP_DIR
from app.models import VideoRequest, ProcessResponse, ProgressResponse
from app.services.download import progress_store, download_video_task
from app.services.media import fetch_media_info
from app.utils.cleanup import cleanup_file

router = APIRouter()


@router.get("/")
async def root():
    return {"message": "YouTube Downloader API", "docs": "/docs"}


@router.post("/fetch")
async def fetch_media(request: VideoRequest):
    """
    Fetch media information without downloading.
    Auto-detects video or photo content.
    Supports: YouTube, TikTok, Twitter/X
    """
    try:
        return fetch_media_info(str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch media info: {str(e)}")


@router.post("/process", response_model=ProcessResponse)
async def process_video(request: VideoRequest, background_tasks: BackgroundTasks):
    """
    Start processing/downloading a video.
    Returns a download_id to track progress.
    """
    download_id = str(uuid.uuid4())

    try:
        # First, get video info
        ydl_opts = YDL_BASE_OPTS.copy()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(str(request.url), download=False)
            sanitized_info = ydl.sanitize_info(info)

        # Initialize progress store
        progress_store[download_id] = {
            'status': 'pending',
            'progress': 0.0,
            'url': str(request.url),
            'format_id': request.format_id,
            'download_mp3': request.download_mp3
        }

        # Start download in background
        background_tasks.add_task(
            download_video_task,
            download_id,
            str(request.url),
            request.format_id,
            request.download_mp3
        )

        return {
            'download_id': download_id,
            'title': sanitized_info.get('title'),
            'duration': int(sanitized_info.get('duration')) if sanitized_info.get('duration') else None,
            'thumbnail': sanitized_info.get('thumbnail'),
            'formats': [
                {
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'resolution': f.get('resolution', 'audio only'),
                }
                for f in sanitized_info.get('formats', [])
            ]
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to process video: {str(e)}")


@router.get("/check-progress/{download_id}", response_model=ProgressResponse)
async def check_progress(download_id: str):
    """
    Check the download progress of a video.
    """
    if download_id not in progress_store:
        raise HTTPException(status_code=404, detail="Download ID not found")

    progress_data = progress_store[download_id]

    return {
        'download_id': download_id,
        'status': progress_data.get('status', 'unknown'),
        'progress': progress_data.get('progress', 0.0),
        'speed': progress_data.get('speed'),
        'eta': progress_data.get('eta'),
        'filename': progress_data.get('filename'),
        'error': progress_data.get('error')
    }


@router.get("/download/{download_id}")
async def download_file(download_id: str, background_tasks: BackgroundTasks):
    """
    Download the processed video file.
    File will be deleted after download completes.
    """
    if download_id not in progress_store:
        raise HTTPException(status_code=404, detail="Download ID not found")

    progress_data = progress_store[download_id]

    if progress_data.get('status') != 'completed':
        raise HTTPException(status_code=400, detail="Download not yet completed")

    final_file = progress_data.get('final_file')
    if not final_file or not os.path.exists(final_file):
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(final_file)

    # Schedule file cleanup after response
    background_tasks.add_task(cleanup_file, download_id, file_path)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type='application/octet-stream'
    )


@router.delete("/cleanup/{download_id}")
async def manual_cleanup(download_id: str):
    """
    Manually cleanup a download entry and its files.
    """
    if download_id not in progress_store:
        raise HTTPException(status_code=404, detail="Download ID not found")

    progress_data = progress_store[download_id]
    final_file = progress_data.get('final_file')

    try:
        if final_file and os.path.exists(final_file):
            Path(final_file).unlink()

        download_dir = TEMP_DIR / download_id
        if download_dir.exists():
            shutil.rmtree(download_dir)

        del progress_store[download_id]

        return {"message": "Cleanup successful"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")


@router.get("/active-downloads")
async def list_active_downloads():
    """
    List all active downloads.
    """
    return {
        "active_downloads": [
            {
                "download_id": did,
                "status": data.get("status"),
                "progress": data.get("progress")
            }
            for did, data in progress_store.items()
        ]
    }
