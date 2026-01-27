#!/usr/bin/env python3
"""
TikTok Downloader Server using yt-dlp (Python Implementation)
100% Feature parity with serverjs
- Embedded yt-dlp (no process spawning)
- Slideshow generation with FFmpeg
- Encryption/decryption support
- Auto cleanup
- Memory efficient with ThreadPoolExecutor
"""

import sys
import os
import io
from pathlib import Path

# Add parent directory to path to import yt_dlp
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Optional, Dict, Any
import logging
from contextlib import asynccontextmanager
import httpx
import threading
from queue import Queue, Empty, Full
import gc

# Import local modules
from encryption import encrypt, decrypt
from cleanup import cleanup_folder, init_cleanup_schedule
from slideshow import create_slideshow, download_file
from config import settings

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Thread pool for blocking operations
executor = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS)

# Global httpx client for connection pooling
http_client: Optional[httpx.AsyncClient] = None

# Ensure temp directory exists
settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    global http_client
    
    # Startup
    logger.info(f"Starting server on port {settings.PORT}")
    logger.info(f"Base URL: {settings.BASE_URL}")
    logger.info(f"Max workers: {settings.MAX_WORKERS}")
    logger.info(f"Temp directory: {settings.TEMP_DIR}")
    
    # Initialize global httpx client with connection pooling
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True
    )
    
    # Initialize cleanup schedule (every 15 minutes)
    cleanup_task = asyncio.create_task(init_cleanup_schedule(settings.TEMP_DIR, "*/15 * * * *"))
    
    yield
    
    # Shutdown
    logger.info("Shutting down server...")
    cleanup_task.cancel()
    
    # Close httpx client
    if http_client:
        await http_client.aclose()
    
    executor.shutdown(wait=True)


# Initialize FastAPI app
app = FastAPI(
    title="TikTok Downloader API",
    description="TikTok video/image downloader using yt-dlp (Python)",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Origin", "Content-Type", "Content-Length", "Accept-Encoding", "Authorization"],
    expose_headers=["Content-Disposition", "X-Filename", "Content-Length"]
)


# Pydantic models
class TikTokRequest(BaseModel):
    url: str


class TikTokResponse(BaseModel):
    status: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# Content type mapping
CONTENT_TYPES = {
    'mp3': ('audio/mpeg', 'mp3'),
    'video': ('video/mp4', 'mp4'),
    'image': ('image/jpeg', 'jpg')
}


def extract_video_info(url: str) -> dict:
    """
    Extract video info using yt-dlp (blocking operation)
    Runs in thread pool
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'socket_timeout': 30,
    }
    
    # Add cookies if available
    cookies_path = settings.TEMP_DIR.parent / 'cookies' / 'www.tiktok.com_cookies.txt'
    if cookies_path.exists():
        ydl_opts['cookiefile'] = str(cookies_path)
    
    ydl = None
    try:
        ydl = yt_dlp.YoutubeDL(ydl_opts)
        info = ydl.extract_info(url, download=False)
        return info
    except Exception as e:
        logger.error(f"yt-dlp extraction failed: {e}")
        raise
    finally:
        # Explicitly close the YoutubeDL instance to release file descriptors
        if ydl:
            try:
                ydl.close()
            except Exception:
                pass
        # Force garbage collection to clean up any lingering resources
        gc.collect()


async def fetch_tiktok_data(url: str) -> dict:
    """Async wrapper for yt-dlp extraction"""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, extract_video_info, url),
            timeout=30.0
        )
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Request timeout after 30 seconds")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def generate_json_response(data: dict, url: str) -> dict:
    """Generate JSON response matching serverjs format"""
    is_image = data.get('formats') and any(
        f.get('format_id', '').startswith('image-') 
        for f in data.get('formats', [])
    )
    
    # Extract author info
    avatar_url = data.get('thumbnails', [{}])[0].get('url', '') if data.get('thumbnails') else ''
    author = {
        'nickname': data.get('uploader') or data.get('channel') or 'unknown',
        'uniqueId': data.get('uploader_id') or data.get('uploader') or 'unknown',
        'signature': data.get('description', ''),
        'avatar': avatar_url,
        'avatarThumb': avatar_url,
        'avatarMedium': avatar_url,
        'avatarLarger': avatar_url
    }
    
    # Extract statistics
    statistics = {
        'play_count': data.get('view_count', 0),
        'digg_count': data.get('like_count', 0),
        'comment_count': data.get('comment_count', 0),
        'share_count': data.get('repost_count', 0)
    }
    
    # Base metadata
    metadata = {
        'title': data.get('title') or data.get('fulltitle', ''),
        'description': data.get('description') or data.get('title', ''),
        'statistics': statistics,
        'artist': data.get('artist') or author['nickname'],
        'cover': data.get('thumbnail', ''),
        'duration': data.get('duration', 0) * 1000 if data.get('duration') else 0,
        'audio': '',
        'download_link': {},
        'music_duration': data.get('duration', 0) * 1000 if data.get('duration') else 0,
        'author': author
    }
    
    if is_image:
        # Photo slideshow post
        formats = data.get('formats', [])
        image_formats = [f for f in formats if f.get('format_id', '').startswith('image-')]
        audio_format = next((f for f in formats if f.get('format_id') == 'audio'), None)
        
        picker = [{'type': 'photo', 'url': img['url']} for img in image_formats]
        
        if audio_format:
            metadata['audio'] = audio_format['url']
        
        # Create encrypted download links for images
        encrypted_image_urls = []
        for img in image_formats:
            encrypted_data = encrypt(
                json.dumps({
                    'url': img['url'],
                    'author': author['nickname'],
                    'type': 'image'
                }),
                settings.ENCRYPTION_KEY,
                360
            )
            encrypted_image_urls.append(f"{settings.BASE_URL}/download?data={encrypted_data}")
        
        metadata['download_link']['no_watermark'] = encrypted_image_urls
        
        if audio_format:
            encrypted_audio = encrypt(
                json.dumps({
                    'url': url,
                    'format_id': audio_format['format_id'],
                    'author': author['nickname']
                }),
                settings.ENCRYPTION_KEY,
                360
            )
            metadata['download_link']['mp3'] = f"{settings.BASE_URL}/stream?data={encrypted_audio}"
        
        # Add slideshow download link
        metadata['download_slideshow_link'] = f"{settings.BASE_URL}/download-slideshow?url={encrypt(url, settings.ENCRYPTION_KEY, 360)}"
        
        return {
            'status': 'picker',
            'photos': picker,
            **metadata
        }
    else:
        # Regular video post
        formats = data.get('formats', [])
        video_formats = [
            f for f in formats 
            if f.get('vcodec') and f['vcodec'] != 'none' and f.get('acodec') and f['acodec'] != 'none'
        ]
        
        # Find audio format
        audio_format = next(
            (f for f in formats if f.get('acodec') and f['acodec'] != 'none' and (not f.get('vcodec') or f['vcodec'] == 'none')),
            None
        )
        
        if not audio_format and video_formats:
            audio_format = video_formats[0]
        
        if audio_format:
            metadata['audio'] = audio_format['url']
        
        # Sort by quality
        video_formats.sort(key=lambda x: (x.get('height', 0) * x.get('width', 0)), reverse=True)
        
        # Find different quality versions
        download_format = next((f for f in formats if f.get('format_id') == 'download'), None)
        hd_formats = [f for f in video_formats if f.get('height', 0) >= 720]
        sd_formats = [f for f in video_formats if f.get('height', 0) < 720]
        
        def generate_download_link(format_obj, file_type='video', use_stream=False):
            if not format_obj:
                return None
            
            # Extract filesize
            filesize = format_obj.get('filesize') or format_obj.get('filesize_approx') or 0

            if use_stream:
                encrypted_data = encrypt(
                    json.dumps({
                        'url': url,
                        'format_id': format_obj['format_id'],
                        'author': author['nickname'],
                        'filesize': filesize
                    }),
                    settings.ENCRYPTION_KEY,
                    360
                )
                return f"{settings.BASE_URL}/stream?data={encrypted_data}"
            else:
                encrypted_data = encrypt(
                    json.dumps({
                        'format_id': format_obj['format_id'],
                        'author': author['nickname'],
                        'type': file_type,
                        'url': format_obj['url'],
                        'filesize': filesize
                    }),
                    settings.ENCRYPTION_KEY,
                    360
                )
                return f"{settings.BASE_URL}/download?data={encrypted_data}"
        
        # Create download links
        metadata['download_link'] = {}
        
        if download_format:
            metadata['download_link']['watermark'] = generate_download_link(download_format, 'video', True)
        
        if sd_formats:
            metadata['download_link']['no_watermark'] = generate_download_link(sd_formats[0], 'video', True)
        
        if hd_formats:
            metadata['download_link']['no_watermark_hd'] = generate_download_link(hd_formats[0], 'video', True)
            if len(hd_formats) > 1:
                metadata['download_link']['watermark_hd'] = generate_download_link(hd_formats[1], 'video', True)
        
        if audio_format:
            metadata['download_link']['mp3'] = generate_download_link(audio_format, 'mp3', True)
        
        # Remove null values
        metadata['download_link'] = {k: v for k, v in metadata['download_link'].items() if v is not None}
        
        return {
            'status': 'tunnel',
            **metadata
        }


@app.post("/tiktok")
async def process_tiktok(request: TikTokRequest):
    """Process TikTok URL and return metadata with encrypted download links"""
    try:
        url = request.url
        
        if not url:
            raise HTTPException(status_code=400, detail="URL parameter is required")
        
        if 'tiktok.com' not in url and 'douyin.com' not in url:
            raise HTTPException(status_code=400, detail="Only TikTok and Douyin URLs are supported")
        
        # Fetch data using yt-dlp
        data = await fetch_tiktok_data(url)
        
        # Generate response
        response = generate_json_response(data, url)
        
        return response
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in TikTok handler: {e}")
        
        error_message = str(e)
        status_code = 500
        
        if 'Unsupported URL' in error_message or 'Unable to download webpage' in error_message:
            status_code = 404
            error_message = 'Video not found. Please check the URL and make sure the video exists.'
        elif 'IP address is blocked' in error_message:
            status_code = 403
            error_message = 'Access denied. The video may be private or restricted.'
        elif 'ERROR:' in error_message:
            match = error_message.split('ERROR:')
            if len(match) > 1:
                error_message = match[1].strip()
        
        raise HTTPException(status_code=status_code, detail=error_message)


@app.get("/download")
async def download_file_endpoint(data: str, request: Request):
    """Download file using encrypted data"""
    try:
        if not data:
            raise HTTPException(status_code=400, detail="Encrypted data parameter is required")
        
        decrypted_data = decrypt(data, settings.ENCRYPTION_KEY)
        download_data = json.loads(decrypted_data)
        
        if not download_data.get('author') or not download_data.get('type'):
            raise HTTPException(status_code=400, detail="Invalid decrypted data: missing author or type")
        
        if download_data['type'] not in CONTENT_TYPES:
            raise HTTPException(status_code=400, detail="Invalid file type specified")
        
        content_type, file_extension = CONTENT_TYPES[download_data['type']]
        # Sanitize filename for iOS compatibility
        import re
        safe_author = re.sub(r'[^a-zA-Z0-9_]', '_', download_data['author'])
        filename = f"{safe_author}.{file_extension}"
        
        if not download_data.get('url'):
            raise HTTPException(status_code=400, detail="No download URL provided")
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Filename': filename
        }
        
        # Set Content-Length if available in token
        filesize = download_data.get('filesize', 0)
        if filesize and filesize > 0:
            headers['Content-Length'] = str(filesize)

        # Use global httpx client for connection pooling
        async def stream_file():
            try:
                async with http_client.stream('GET', download_data['url']) as response:
                    # Forward content length if available and not already set
                    if 'content-length' in response.headers and 'Content-Length' not in headers:
                        headers['Content-Length'] = response.headers['content-length']
                    
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        # Check if client disconnected
                        if await request.is_disconnected():
                            logger.info("Client disconnected during download")
                            break
                        yield chunk
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error during download: {e}")
                raise
            except Exception as e:
                logger.error(f"Error streaming file: {e}")
                raise
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Filename': filename
        }
        
        return StreamingResponse(
            stream_file(),
            media_type=content_type,
            headers=headers
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in download handler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download-slideshow")
async def download_slideshow(url: str, background_tasks: BackgroundTasks):
    """Generate and download slideshow video from image post"""
    work_dir = None
    
    try:
        if not url:
            raise HTTPException(status_code=400, detail="URL parameter is required")
        
        # Decrypt URL
        decrypted_url = decrypt(url, settings.ENCRYPTION_KEY)
        
        # Fetch TikTok data
        data = await fetch_tiktok_data(decrypted_url)
        
        if not data:
            raise HTTPException(status_code=500, detail="Invalid response from yt-dlp")
        
        # Check if it's an image post
        is_image = data.get('formats') and any(
            f.get('format_id', '').startswith('image-') 
            for f in data.get('formats', [])
        )
        
        if not is_image:
            raise HTTPException(status_code=400, detail="Only image posts are supported")
        
        # Create work directory
        video_id = data.get('id', 'unknown')
        author_id = data.get('uploader_id', 'unknown')
        folder_name = f"{video_id}_{author_id}_{asyncio.get_event_loop().time()}"
        work_dir = settings.TEMP_DIR / folder_name
        work_dir.mkdir(parents=True, exist_ok=True)
        
        # Get image and audio URLs
        formats = data.get('formats', [])
        image_formats = [f for f in formats if f.get('format_id', '').startswith('image-')]
        audio_format = next((f for f in formats if f.get('format_id') == 'audio'), None)
        
        if not image_formats:
            raise HTTPException(status_code=400, detail="No images found")
        
        if not audio_format or not audio_format.get('url'):
            raise HTTPException(status_code=400, detail="Could not find audio URL")
        
        image_urls = [f['url'] for f in image_formats]
        audio_url = audio_format['url']
        
        # Download audio and images
        audio_path = work_dir / 'audio.mp3'
        
        loop = asyncio.get_event_loop()
        
        # Download audio
        await loop.run_in_executor(executor, download_file, audio_url, str(audio_path))
        
        # Download images
        image_paths = []
        for i, img_url in enumerate(image_urls):
            img_path = work_dir / f'image_{i}.jpg'
            await loop.run_in_executor(executor, download_file, img_url, str(img_path))
            image_paths.append(str(img_path))
        
        # Create slideshow
        output_path = work_dir / 'slideshow.mp4'
        await loop.run_in_executor(
            executor,
            create_slideshow,
            image_paths,
            str(audio_path),
            str(output_path)
        )
        
        # Prepare response
        author_nickname = data.get('uploader') or data.get('channel') or 'unknown'
        sanitized = ''.join(c if c.isalnum() else '_' for c in author_nickname)
        filename = f"{sanitized}_{int(asyncio.get_event_loop().time())}.mp4"
        
        # Schedule cleanup after response
        background_tasks.add_task(cleanup_folder, str(work_dir))
        
        return FileResponse(
            path=str(output_path),
            media_type='video/mp4',
            filename=filename,
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in slideshow handler: {e}")
        if work_dir and work_dir.exists():
            await asyncio.get_event_loop().run_in_executor(executor, cleanup_folder, str(work_dir))
        raise HTTPException(status_code=500, detail=str(e))


def download_with_stdout_streaming(url, format_id, chunk_queue, error_dict, stop_event):
    """Download using yt-dlp with direct streaming"""
    ydl = None
    queue_writer = None
    
    try:
        logger.info(f"Starting yt-dlp streaming for format: {format_id}")
        
        # Create a custom file-like object that writes to queue
        class QueueWriter:
            def __init__(self, queue, stop_signal):
                self.queue = queue
                self.stop_signal = stop_signal
                self.buffer = bytearray()
                self.encoding = 'utf-8'
                self.mode = 'wb'
                self.name = '<queue>'
                self.closed = False
            
            def _put_chunk(self, chunk):
                """Put chunk into queue with cancellation support"""
                while True:
                    if self.stop_signal.is_set():
                        raise RuntimeError("Stream cancelled by client")
                    try:
                        self.queue.put(chunk, timeout=1)
                        return
                    except Full:
                        continue

            def write(self, data):
                """Write data to queue in chunks"""
                if self.closed:
                    raise ValueError("I/O operation on closed file")
                
                if isinstance(data, str):
                    data = data.encode('utf-8')
                
                self.buffer.extend(data)
                
                # Send in 8KB chunks
                while len(self.buffer) >= 8192:
                    chunk = bytes(self.buffer[:8192])
                    self.buffer = self.buffer[8192:]
                    self._put_chunk(chunk)
                
                return len(data)
            
            def flush(self):
                """Flush remaining buffer"""
                if not self.closed and self.buffer:
                    self._put_chunk(bytes(self.buffer))
                    self.buffer = bytearray()
            
            def close(self):
                """Close and flush"""
                if not self.closed:
                    self.flush()
                    self.closed = True
            
            def writable(self):
                return not self.closed
            
            def readable(self):
                return False
            
            def seekable(self):
                return False
        
        # Configure yt-dlp to output to custom stream
        queue_writer = QueueWriter(chunk_queue, stop_event)
        
        ydl_opts = {
            'format': format_id,
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'outtmpl': '-',
            'logtostderr': True,
            'output_stream': queue_writer  # Use our custom stream!
        }
        
        # Create YoutubeDL instance
        ydl = yt_dlp.YoutubeDL(ydl_opts)
        
        logger.info("Downloading via yt-dlp...")
        
        # Download - this will write to our queue_writer
        ydl.download([url])
        
        # Flush everything
        queue_writer.flush()
        
        logger.info("yt-dlp download completed")
        
        # Signal end of stream (non-blocking if queue is full)
        try:
            chunk_queue.put(None, timeout=1)
        except Full:
            pass
        
    except Exception as e:
        logger.error(f"Error in yt-dlp streaming: {e}")
        logger.exception(e)  # Log full traceback
        error_dict['error'] = str(e)
        # Signal end of stream (non-blocking if queue is full)
        try:
            chunk_queue.put(None, timeout=1)
        except Full:
            pass
    finally:
        # CRITICAL: Close the YoutubeDL instance to release file descriptors
        if ydl:
            try:
                ydl.close()
            except Exception:
                pass
        
        # Close the queue writer
        if queue_writer:
            try:
                queue_writer.close()
            except Exception:
                pass
        
        # Force garbage collection
        gc.collect()


@app.get("/stream")
async def stream_video(data: str, request: Request):
    """Stream video directly using yt-dlp stdout"""
    try:
        if not data:
            raise HTTPException(status_code=400, detail="Encrypted data parameter is required")
        
        decrypted_data = decrypt(data, settings.ENCRYPTION_KEY)
        stream_data = json.loads(decrypted_data)
        
        if not stream_data.get('url') or not stream_data.get('format_id') or not stream_data.get('author'):
            raise HTTPException(
                status_code=400,
                detail="Invalid decrypted data: missing url, format_id, or author"
            )
        
        # Determine file extension and content type
        ext = 'mp3' if 'audio' in stream_data['format_id'] else 'mp4'
        content_type = 'audio/mpeg' if ext == 'mp3' else 'video/mp4'
        import re
        safe_author = re.sub(r'[^a-zA-Z0-9_]', '_', stream_data['author'])
        filename = f"{safe_author}.{ext}"
        
        chunk_queue = Queue(maxsize=20)
        download_error = {'error': None}
        stop_event = threading.Event()
        
        # Start download in background thread
        download_thread = threading.Thread(
            target=download_with_stdout_streaming,
            args=(stream_data['url'], stream_data['format_id'], chunk_queue, download_error, stop_event),
            daemon=True
        )
        download_thread.start()
        
        async def stream_content():
            """Stream chunks from queue"""
            loop = asyncio.get_event_loop()
            client_disconnected = False
            
            try:
                while True:
                    # Check if client disconnected
                    if await request.is_disconnected():
                        logger.info("Client disconnected during stream")
                        client_disconnected = True
                        stop_event.set()
                        break
                    
                    try:
                        # Get chunk from queue
                        chunk = await loop.run_in_executor(
                            None,
                            lambda: chunk_queue.get(timeout=30)
                        )
                        
                        if chunk is None:  # End signal
                            if download_error['error']:
                                logger.error(f"Stream error: {download_error['error']}")
                            break
                        
                        yield chunk
                        
                    except Empty:
                        logger.warning("Stream timeout waiting for chunk")
                        stop_event.set()
                        break
                    except Exception as e:
                        logger.error(f"Error yielding chunk: {e}")
                        stop_event.set()
                        break
            finally:
                # Signal producer to stop and drain the queue if client disconnected
                stop_event.set()
                if client_disconnected:
                    try:
                        while True:
                            chunk_queue.get_nowait()
                            if chunk is None:
                                break
                    except Empty:
                        pass
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Filename': filename,
            'Cache-Control': 'no-cache',
        }
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Filename': filename,
            'Cache-Control': 'no-cache',
        }
        
        # Set Content-Length if available (critical for iOS)
        filesize = stream_data.get('filesize', 0)
        if filesize and filesize > 0:
            headers['Content-Length'] = str(filesize)
        
        return StreamingResponse(
            stream_content(),
            media_type=content_type,
            headers=headers
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in stream handler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    import resource
    
    # Get current file descriptor usage
    try:
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception:
        soft_limit, hard_limit = -1, -1
    
    # Count open file descriptors (Linux/Mac)
    try:
        import os
        fd_count = len(os.listdir('/proc/self/fd')) if os.path.exists('/proc/self/fd') else -1
    except Exception:
        fd_count = -1
    
    health = {
        'status': 'ok',
        'time': asyncio.get_event_loop().time(),
        'ytdlp': 'unknown',
        'workers': {
            'max': settings.MAX_WORKERS,
            'active': len([t for t in executor._threads if t.is_alive()]) if hasattr(executor, '_threads') else 0
        },
        'file_descriptors': {
            'current': fd_count,
            'soft_limit': soft_limit,
            'hard_limit': hard_limit
        }
    }
    
    try:
        health['ytdlp'] = yt_dlp.version.__version__
    except Exception as e:
        health['ytdlp'] = f'error: {e}'
    
    # Trigger garbage collection on health check to help clean up
    gc.collect()
    
    return health


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={'error': 'Route not found'}
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            'error': 'Unexpected server error',
            'message': str(exc)
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.PORT,
        log_level="info"
    )
