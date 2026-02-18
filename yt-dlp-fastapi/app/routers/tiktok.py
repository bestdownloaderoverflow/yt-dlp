import asyncio
import uuid
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

import yt_dlp

from app.config import YDL_BASE_OPTS, TEMP_DIR
from app.models import TikTokRequest
from app.utils.cleanup import scheduled_cleanup
from app.utils.slideshow import download_file_sync, create_slideshow

router = APIRouter(prefix="/tiktok", tags=["TikTok"])


@router.post("/download-photo/{photo_index}")
async def download_tiktok_photo(request: TikTokRequest, photo_index: int, background_tasks: BackgroundTasks):
    """
    Download a single photo from TikTok slideshow.
    """
    download_id = str(uuid.uuid4())
    work_dir = TEMP_DIR / download_id
    
    try:
        url = str(request.url)
        work_dir.mkdir(parents=True, exist_ok=True)
        
        # Schedule auto cleanup (5 minutes)
        asyncio.create_task(scheduled_cleanup(work_dir, is_file=False))
        
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


@router.post("/download-slideshow")
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
        
        # Schedule auto cleanup (5 minutes)
        asyncio.create_task(scheduled_cleanup(work_dir, is_file=False))
        
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
