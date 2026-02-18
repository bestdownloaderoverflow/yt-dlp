import asyncio
from typing import Optional, Dict, Any
from pathlib import Path

import yt_dlp

from app.config import YDL_BASE_OPTS, TEMP_DIR


# Store download progress
progress_store: Dict[str, Dict[str, Any]] = {}


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


async def download_video_task(download_id: str, url: str, format_id: Optional[str], download_mp3: bool):
    """Background task to download video."""
    # Import here to avoid circular import
    from app.utils.cleanup import scheduled_cleanup
    
    download_dir = TEMP_DIR / download_id
    download_dir.mkdir(exist_ok=True)

    # Schedule auto cleanup for this download directory (5 minutes)
    asyncio.create_task(scheduled_cleanup(download_dir, is_file=False))

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
            # Also schedule cleanup for the file itself
            asyncio.create_task(scheduled_cleanup(files[0], is_file=True))
        else:
            progress_store[download_id]['status'] = 'error'
            progress_store[download_id]['error'] = 'No file found after download'

    except Exception as e:
        progress_store[download_id]['status'] = 'error'
        progress_store[download_id]['error'] = str(e)
