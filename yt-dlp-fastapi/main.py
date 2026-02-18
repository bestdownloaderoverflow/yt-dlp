import os
import sys
import json
import asyncio
import uuid
import shutil
import subprocess
import httpx
from typing import Optional, Dict, Any, Union, List
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl

# Use local yt_dlp fork
sys.path.insert(0, '/Users/almafazi/Documents/yt-dlp-tiktok')
import yt_dlp


# ============ SLIDESHOW UTILITIES ============

def download_file_sync(url: str, output_path: Path, timeout: int = 120) -> str:
    """Download file from URL to local path synchronously"""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream('GET', url) as response:
                response.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
        return str(output_path)
    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        raise Exception(f"Failed to download file: {e}")


def create_slideshow(
    image_paths: List[str],
    audio_path: str,
    output_path: str,
    duration_per_image: int = 4
) -> None:
    """Create a slideshow video from images and audio using FFmpeg"""
    if not image_paths:
        raise ValueError("No image paths provided")
    
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    for img_path in image_paths:
        if not Path(img_path).exists():
            raise FileNotFoundError(f"Image file not found: {img_path}")
    
    try:
        cmd = ['ffmpeg', '-y']
        
        # Add each image as input with duration
        for img_path in image_paths:
            cmd.extend([
                '-loop', '1',
                '-t', str(duration_per_image),
                '-i', img_path
            ])
        
        # Add audio with loop
        cmd.extend([
            '-stream_loop', '-1',
            '-i', audio_path
        ])
        
        # Build complex filter
        filter_parts = []
        
        # Scale and pad each image to 1080x1920 (portrait)
        for i in range(len(image_paths)):
            filter_parts.append(
                f'[{i}:v]scale=w=1080:h=1920:force_original_aspect_ratio=decrease,'
                f'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]'
            )
        
        # Concatenate all scaled/padded video streams
        concat_inputs = ''.join(f'[v{i}]' for i in range(len(image_paths)))
        filter_parts.append(f'{concat_inputs}concat=n={len(image_paths)}:v=1:a=0[vout]')
        
        # Calculate total video duration
        video_duration = len(image_paths) * duration_per_image
        
        # Trim audio to video duration
        filter_parts.append(f'[{len(image_paths)}:a]atrim=0:{video_duration}[aout]')
        
        # Join filter parts
        filter_complex = ';'.join(filter_parts)
        
        # Add filter and output options
        cmd.extend([
            '-filter_complex', filter_complex,
            '-map', '[vout]',
            '-map', '[aout]',
            '-pix_fmt', 'yuv420p',
            '-fps_mode', 'cfr',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            output_path
        ])
        
        # Run FFmpeg
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            raise Exception(f"FFmpeg failed: {result.stderr}")
        
        if not Path(output_path).exists():
            raise Exception("Output file was not created")
            
    except subprocess.TimeoutExpired:
        raise Exception("Slideshow creation timeout after 5 minutes")
    except Exception as e:
        output = Path(output_path)
        if output.exists():
            output.unlink()
        raise

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


class TikTokRequest(BaseModel):
    url: HttpUrl


class ProcessResponse(BaseModel):
    download_id: str
    title: str
    duration: Optional[Union[int, float]]
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
async def fetch_media_info(request: VideoRequest):
    """
    Fetch media information without downloading.
    Auto-detects video or photo content.
    Supports: YouTube, TikTok, Twitter/X
    """
    try:
        ydl_opts = YDL_BASE_OPTS.copy()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(str(request.url), download=False)
            sanitized_info = ydl.sanitize_info(info)

            # Detect content type
            is_playlist = sanitized_info.get('_type') == 'playlist'
            entries = sanitized_info.get('entries', [])
            
            # Check if it's a photo gallery (Twitter/X)
            if is_playlist and entries:
                # Check first entry to determine if photo or video
                first_entry = entries[0]
                first_formats = first_entry.get('formats', [])
                is_photo_gallery = any(
                    (f.get('video_ext') or '').lower() in ('jpg', 'jpeg', 'png', 'webp', 'gif')
                    for f in first_formats
                )
                
                if is_photo_gallery:
                    # Return photo gallery response
                    photos = []
                    for idx, entry in enumerate(entries):
                        formats = entry.get('formats', [])
                        best_format = next(
                            (f for f in formats if f.get('format_id') == 'orig'),
                            formats[0] if formats else None
                        )
                        if best_format:
                            photos.append({
                                'index': idx,
                                'entry_id': entry.get('id'),
                                'url': best_format.get('url'),
                                'width': entry.get('width') or best_format.get('width'),
                                'height': entry.get('height') or best_format.get('height'),
                                'thumbnail': entry.get('thumbnail'),
                            })
                    
                    return {
                        'content_type': 'photo_gallery',
                        'platform': detect_platform(str(request.url)),
                        'title': sanitized_info.get('title'),
                        'uploader': sanitized_info.get('uploader'),
                        'photo_count': len(photos),
                        'photos': photos,
                        'original_url': str(request.url),
                    }
                else:
                    # Video playlist
                    videos = []
                    for idx, entry in enumerate(entries):
                        formats = entry.get('formats', [])
                        video_formats = [f for f in formats if f.get('vcodec') and f['vcodec'] != 'none']
                        if video_formats:
                            best = max(video_formats, key=lambda x: x.get('height', 0) * x.get('width', 0))
                            videos.append({
                                'index': idx,
                                'entry_id': entry.get('id'),
                                'title': entry.get('title'),
                                'url': best.get('url'),
                                'resolution': best.get('resolution'),
                            })
                    
                    return {
                        'content_type': 'video_playlist',
                        'platform': detect_platform(str(request.url)),
                        'title': sanitized_info.get('title'),
                        'uploader': sanitized_info.get('uploader'),
                        'video_count': len(videos),
                        'videos': videos,
                        'original_url': str(request.url),
                    }
            
            # Single media (not playlist)
            formats = sanitized_info.get('formats', [])
            
            # Check if it's a photo (single image)
            image_formats = [f for f in formats if (f.get('video_ext') or '').lower() in ('jpg', 'jpeg', 'png', 'webp', 'gif')]
            if image_formats:
                return {
                    'content_type': 'photo',
                    'platform': detect_platform(str(request.url)),
                    'title': sanitized_info.get('title'),
                    'uploader': sanitized_info.get('uploader'),
                    'photos': [{
                        'index': 0,
                        'url': f.get('url'),
                        'width': f.get('width'),
                        'height': f.get('height'),
                    } for f in image_formats],
                    'original_url': str(request.url),
                }
            
            # Check TikTok slideshow
            tiktok_images = [f for f in formats if f.get('format_id', '').startswith('image-')]
            if tiktok_images:
                audio_format = next((f for f in formats if f.get('format_id') == 'audio'), None)
                return {
                    'content_type': 'slideshow',
                    'platform': 'tiktok',
                    'title': sanitized_info.get('title'),
                    'uploader': sanitized_info.get('uploader'),
                    'photo_count': len(tiktok_images),
                    'photos': [{'index': i, 'url': img.get('url')} for i, img in enumerate(tiktok_images)],
                    'audio_url': audio_format.get('url') if audio_format else None,
                    'original_url': str(request.url),
                }
            
            # Regular video
            video_formats = []
            for f in formats:
                if f.get('vcodec') and f['vcodec'] != 'none':
                    video_formats.append({
                        'format_id': f.get('format_id'),
                        'ext': f.get('ext'),
                        'resolution': f.get('resolution'),
                        'filesize': f.get('filesize') or f.get('filesize_approx'),
                        'url': f.get('url'),
                    })
            
            return {
                'content_type': 'video',
                'platform': detect_platform(str(request.url)),
                'title': sanitized_info.get('title'),
                'description': sanitized_info.get('description'),
                'duration': int(sanitized_info.get('duration')) if sanitized_info.get('duration') else None,
                'uploader': sanitized_info.get('uploader'),
                'thumbnail': sanitized_info.get('thumbnail'),
                'formats': video_formats,
                'original_url': str(request.url),
            }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch media info: {str(e)}")


def detect_platform(url: str) -> str:
    """Detect platform from URL"""
    url_lower = url.lower()
    if 'tiktok.com' in url_lower or 'douyin.com' in url_lower:
        return 'tiktok'
    elif 'twitter.com' in url_lower or 'x.com' in url_lower:
        return 'twitter'
    elif 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    return 'unknown'


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


# ============ TIKTOK PHOTO & SLIDESHOW ENDPOINTS ============

@app.post("/tiktok/download-photo/{photo_index}")
async def download_tiktok_photo(request: TikTokRequest, photo_index: int, background_tasks: BackgroundTasks):
    """
    Download a single photo from TikTok slideshow.
    """
    download_id = str(uuid.uuid4())
    work_dir = TEMP_DIR / download_id
    
    try:
        url = str(request.url)
        work_dir.mkdir(parents=True, exist_ok=True)
        
        ydl_opts = YDL_BASE_OPTS.copy()
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = info.get('formats', [])
            image_formats = [f for f in formats if f.get('format_id', '').startswith('image-')]
            
            if photo_index >= len(image_formats):
                raise HTTPException(status_code=400, detail=f"Photo index {photo_index} out of range. Only {len(image_formats)} photos available.")
            
            photo = image_formats[photo_index]
            photo_url = photo.get('url')
            ext = photo.get('ext', 'jpg')
            
            # Download photo
            author = info.get('uploader', 'tiktok')
            filename = f"{author}_photo_{photo_index}.{ext}"
            output_path = work_dir / filename
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, download_file_sync, photo_url, output_path)
            
            # Schedule cleanup
            background_tasks.add_task(cleanup_photo_dir, work_dir)
            
            return FileResponse(
                path=str(output_path),
                filename=filename,
                media_type=f'image/{ext}' if ext != 'jpg' else 'image/jpeg'
            )
            
    except HTTPException:
        raise
    except Exception as e:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        raise HTTPException(status_code=500, detail=f"Failed to download photo: {str(e)}")


async def cleanup_photo_dir(work_dir: Path):
    """Clean up photo download directory"""
    await asyncio.sleep(5)
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
    except Exception as e:
        print(f"Cleanup error: {e}")


@app.post("/tiktok/download-slideshow")
async def download_tiktok_slideshow(request: TikTokRequest, background_tasks: BackgroundTasks):
    """
    Download TikTok slideshow as a video (images + audio combined using FFmpeg).
    Downloads all files locally first, then creates slideshow.
    """
    download_id = str(uuid.uuid4())
    work_dir = TEMP_DIR / f"slideshow_{download_id}"
    
    try:
        url = str(request.url)
        work_dir.mkdir(parents=True, exist_ok=True)
        
        ydl_opts = YDL_BASE_OPTS.copy()
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = info.get('formats', [])
            image_formats = [f for f in formats if f.get('format_id', '').startswith('image-')]
            audio_format = next((f for f in formats if f.get('format_id') == 'audio'), None)
            
            if not image_formats:
                raise HTTPException(status_code=400, detail="No photos found in this TikTok post")
            
            if not audio_format:
                raise HTTPException(status_code=400, detail="No audio found for slideshow")
            
            # Download images
            image_paths = []
            loop = asyncio.get_event_loop()
            
            for i, img in enumerate(image_formats):
                img_path = work_dir / f"image_{i}.jpg"
                await loop.run_in_executor(None, download_file_sync, img.get('url'), img_path)
                image_paths.append(str(img_path))
            
            # Download audio
            audio_path = work_dir / "audio.mp3"
            await loop.run_in_executor(None, download_file_sync, audio_format.get('url'), audio_path)
            
            # Create slideshow
            output_path = work_dir / "slideshow.mp4"
            await loop.run_in_executor(
                None,
                create_slideshow,
                image_paths,
                str(audio_path),
                str(output_path),
                4  # 4 seconds per image
            )
            
            # Prepare response
            author = info.get('uploader', 'tiktok')
            filename = f"{author}_slideshow.mp4"
            
            # Schedule cleanup
            background_tasks.add_task(cleanup_slideshow_dir, work_dir)
            
            return FileResponse(
                path=str(output_path),
                filename=filename,
                media_type='video/mp4'
            )
            
    except HTTPException:
        raise
    except Exception as e:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        raise HTTPException(status_code=500, detail=f"Failed to create slideshow: {str(e)}")


async def cleanup_slideshow_dir(work_dir: Path):
    """Clean up slideshow directory after delay"""
    await asyncio.sleep(10)
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
    except Exception as e:
        print(f"Slideshow cleanup error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
