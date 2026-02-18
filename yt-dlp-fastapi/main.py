import os
import json
import asyncio
import uuid
import shutil
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, HttpUrl
import yt_dlp

app = FastAPI(title="YouTube Downloader API", version="1.0.0")

# yt-dlp options - JS runtime auto-detected by yt-dlp 2026+
YDL_BASE_OPTS = {
    'quiet': True,
    'no_warnings': True,
}

TEMP_DIR = Path("./temp_downloads")
TEMP_DIR.mkdir(exist_ok=True)

# Store download progress
progress_store: Dict[str, Dict[str, Any]] = {}


class VideoRequest(BaseModel):
    url: HttpUrl
    format_id: Optional[str] = None
    download_mp3: bool = False


class ProcessResponse(BaseModel):
    download_id: str
    title: str
    duration: Optional[int]
    thumbnail: Optional[str]
    formats: list


class ProgressResponse(BaseModel):
    download_id: str
    status: str
    progress: float
    speed: Optional[str]
    eta: Optional[str]
    filename: Optional[str]
    error: Optional[str]


def progress_hook(download_id: str):
    def hook(d):
        if download_id not in progress_store:
            progress_store[download_id] = {}

        if d['status'] == 'downloading':
            # Calculate progress percentage
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            if total > 0:
                progress = (downloaded / total) * 100
            else:
                progress = 0.0

            # Get speed and eta
            speed = d.get('_speed_str') or d.get('speed_string') or 'N/A'
            eta = d.get('_eta_str') or d.get('eta_string') or 'N/A'

            progress_store[download_id].update({
                'status': 'downloading',
                'progress': round(progress, 2),
                'speed': speed,
                'eta': eta,
                'filename': d.get('filename'),
                'downloaded_bytes': downloaded,
                'total_bytes': total,
                'fragment_index': d.get('fragment_index'),
                'fragment_count': d.get('fragment_count')
            })
        elif d['status'] == 'finished':
            progress_store[download_id].update({
                'status': 'finished',
                'progress': 100.0,
                'filename': d.get('filename'),
                'total_bytes': d.get('total_bytes')
            })
        elif d['status'] == 'error':
            progress_store[download_id].update({
                'status': 'error',
                'error': str(d.get('error', 'Unknown error'))
            })
    return hook


@app.get("/")
async def root():
    return {"message": "YouTube Downloader API", "docs": "/docs"}


@app.post("/fetch")
async def fetch_video_info(request: VideoRequest):
    """
    Fetch video information without downloading.
    Returns video metadata including available formats.
    """
    try:
        ydl_opts = YDL_BASE_OPTS.copy()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(str(request.url), download=False)

            # Sanitize info for JSON response
            sanitized_info = ydl.sanitize_info(info)

            # Extract relevant format information
            formats = []
            for f in sanitized_info.get('formats', []):
                format_info = {
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'resolution': f.get('resolution', 'audio only'),
                    'filesize': f.get('filesize') or f.get('filesize_approx'),
                    'vcodec': f.get('vcodec', 'none'),
                    'acodec': f.get('acodec', 'none'),
                    'abr': f.get('abr'),
                    'vbr': f.get('vbr'),
                    'fps': f.get('fps'),
                }
                formats.append(format_info)

            return {
                'title': sanitized_info.get('title'),
                'description': sanitized_info.get('description'),
                'duration': sanitized_info.get('duration'),
                'thumbnail': sanitized_info.get('thumbnail'),
                'uploader': sanitized_info.get('uploader'),
                'upload_date': sanitized_info.get('upload_date'),
                'view_count': sanitized_info.get('view_count'),
                'formats': formats,
                'original_url': sanitized_info.get('original_url')
            }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch video info: {str(e)}")


@app.post("/process", response_model=ProcessResponse)
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
            'duration': sanitized_info.get('duration'),
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


async def download_video_task(download_id: str, url: str, format_id: Optional[str], download_mp3: bool):
    """Background task to download video."""
    download_dir = TEMP_DIR / download_id
    download_dir.mkdir(exist_ok=True)

    try:
        ydl_opts = YDL_BASE_OPTS.copy()
        ydl_opts.update({
            'outtmpl': str(download_dir / '%(title)s.%(ext)s'),
            'progress_hooks': [progress_hook(download_id)],
            'noprogress': False,
            'verbose': True,
        })

        if download_mp3:
            # Download and convert to MP3
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        elif format_id:
            # Download specific format
            ydl_opts['format'] = format_id
        else:
            # Download best quality
            ydl_opts['format'] = 'best'

        # Run download in thread pool to not block
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await loop.run_in_executor(None, ydl.download, [url])

        # Find the downloaded file
        files = list(download_dir.iterdir())
        if files:
            progress_store[download_id]['final_file'] = str(files[0])
            progress_store[download_id]['status'] = 'completed'
        else:
            progress_store[download_id]['status'] = 'error'
            progress_store[download_id]['error'] = 'No file found after download'

    except Exception as e:
        progress_store[download_id]['status'] = 'error'
        progress_store[download_id]['error'] = str(e)


@app.get("/check-progress/{download_id}", response_model=ProgressResponse)
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


@app.get("/download/{download_id}")
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


async def cleanup_file(download_id: str, file_path: Path):
    """Clean up temporary files after download."""
    # Wait a bit to ensure file is sent
    await asyncio.sleep(5)

    try:
        # Remove the file
        if file_path.exists():
            file_path.unlink()

        # Remove the directory
        download_dir = TEMP_DIR / download_id
        if download_dir.exists():
            shutil.rmtree(download_dir)

        # Clean up progress store
        if download_id in progress_store:
            del progress_store[download_id]

    except Exception as e:
        print(f"Cleanup error for {download_id}: {e}")


@app.delete("/cleanup/{download_id}")
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


@app.get("/active-downloads")
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
