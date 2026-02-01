#!/usr/bin/env python3
"""
TikTok/X (Twitter) Downloader API using yt-dlp Python wrapper
Simple API that returns video/photo data with direct download links

Endpoints:
  POST /download - Extract video/photo info and return download links
  GET  /health   - Health check
"""

import sys
import os
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime

# Add parent directory to path to import yt_dlp
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import yt_dlp
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Thread pool for blocking yt-dlp operations
executor = ThreadPoolExecutor(max_workers=4)

# FastAPI app
app = FastAPI(
    title="TikTok/X Video Downloader API",
    description="Simple API to extract video/photo info and download links from TikTok and X (Twitter)",
    version="1.1.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"]
)


# ============= Pydantic Models =============

class DownloadRequest(BaseModel):
    url: str = Field(..., description="TikTok or X (Twitter) URL to extract")


class VideoFormat(BaseModel):
    quality: str
    resolution: str
    url: str
    size_bytes: Optional[int] = None
    format_id: str


class MediaEntry(BaseModel):
    """Individual media entry for playlist/multiple images"""
    entry_id: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    duration_formatted: Optional[str] = None
    media_type: str = "video"  # "video", "photo", "audio"
    formats: List[VideoFormat] = []
    best_url: Optional[str] = None


class VideoData(BaseModel):
    platform: str  # "tiktok" or "x"
    content_type: str = "video"  # "video", "photo", "playlist", "audio"
    video_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    author_name: Optional[str] = None
    author_username: Optional[str] = None
    author_avatar: Optional[str] = None
    thumbnail: Optional[str] = None
    duration_seconds: Optional[float] = None
    duration_formatted: Optional[str] = None
    stats: Dict[str, Optional[int]] = {}
    created_at: Optional[str] = None
    original_url: str
    # For playlist/multiple media
    is_playlist: bool = False
    playlist_count: Optional[int] = None
    entries: List[MediaEntry] = []


class DownloadResponse(BaseModel):
    success: bool
    message: str
    data: Optional[VideoData] = None
    video_formats: List[VideoFormat] = []  # For backward compatibility
    audio_formats: List[VideoFormat] = []  # For backward compatibility
    image_formats: List[VideoFormat] = []  # For photo posts
    best_video_url: Optional[str] = None   # For backward compatibility
    best_audio_url: Optional[str] = None   # For backward compatibility
    best_image_url: Optional[str] = None   # For photo posts
    extracted_at: str


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    error_code: Optional[str] = None


# ============= Helper Functions =============

def format_duration(seconds: Optional[float]) -> Optional[str]:
    """Format duration in seconds to MM:SS or HH:MM:SS"""
    if not seconds:
        return None
    
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_formats(formats: List[Dict]) -> tuple[List[VideoFormat], List[VideoFormat], List[VideoFormat]]:
    """
    Parse yt-dlp formats into video, audio, and image formats
    Returns: (video_formats, audio_formats, image_formats)
    """
    video_formats = []
    audio_formats = []
    image_formats = []
    progressive_formats = []  # Combined video+audio
    
    # Track seen qualities to avoid duplicates
    seen_video_qualities = set()
    seen_audio_qualities = set()
    seen_progressive = set()
    seen_image_qualities = set()
    
    for fmt in formats:
        format_id = fmt.get('format_id', '')
        vcodec = (fmt.get('vcodec') or 'none').lower()
        acodec = (fmt.get('acodec') or 'none').lower()
        height = fmt.get('height') or 0
        width = fmt.get('width') or 0
        url = fmt.get('url', '')
        resolution = fmt.get('resolution', '')
        video_ext = (fmt.get('video_ext') or '').lower()
        
        if not url:
            continue
        
        # Detect protocol
        protocol = fmt.get('protocol', '')
        is_http = protocol == 'https' or (url.startswith('http') and not '.m3u8' in url)
        is_hls = '.m3u8' in url.lower() or protocol in ('m3u8', 'm3u8_native')
        
        # Category 0: Image format (photo posts from X/Twitter)
        # Photos have video_ext like 'jpg' and are HTTP direct links
        is_image_format = video_ext in ('jpg', 'jpeg', 'png', 'webp', 'gif') and is_http
        
        # Category 1: True audio-only format (vcodec is 'none' and format_id indicates audio)
        is_audio_format = vcodec == 'none' and ('audio' in format_id.lower() or resolution == 'audio only')
        
        # Category 2: Progressive/HTTP combined format (has both video and audio)
        is_combined = is_http and height > 0 and not is_image_format
        
        # Category 3: HLS video-only format (acodec is 'none')
        is_video_only = is_hls and vcodec != 'none' and height > 0
        
        if is_image_format:
            # Image format (photo post)
            resolution_str = f"{width}x{height}" if width and height else str(resolution)
            quality_key = f"{width}x{height}_{format_id}"
            
            if quality_key in seen_image_qualities:
                continue
            seen_image_qualities.add(quality_key)
            
            # Quality label based on format_id (orig, large, medium, small, thumb)
            quality_label = format_id.upper() if format_id else "IMAGE"
            
            image_formats.append(VideoFormat(
                quality=quality_label,
                resolution=resolution_str,
                url=url,
                size_bytes=fmt.get('filesize') or fmt.get('filesize_approx'),
                format_id=format_id
            ))
        
        elif is_audio_format:
            # Extract bitrate from various fields
            abr = fmt.get('abr') or fmt.get('tbr') or 0
            
            # Try to extract from format_id for X/Twitter (e.g., "hls-audio-128000-Audio")
            if not abr and 'audio' in format_id.lower():
                import re
                match = re.search(r'audio-(\d+)', format_id.lower())
                if match:
                    abr = int(match.group(1)) // 1000  # Convert to kbps
            
            quality = f"{int(abr)}kbps" if abr and abr > 0 else "audio"
            
            if quality in seen_audio_qualities:
                continue
            seen_audio_qualities.add(quality)
            
            audio_formats.append(VideoFormat(
                quality=quality,
                resolution="audio only",
                url=url,
                size_bytes=fmt.get('filesize') or fmt.get('filesize_approx'),
                format_id=format_id
            ))
        
        elif is_combined:
            # Progressive download (video + audio combined)
            quality_label = f"{height}p"
            resolution_str = f"{width}x{height}" if width and height else str(resolution)
            
            if height in seen_progressive:
                continue
            seen_progressive.add(height)
            
            progressive_formats.append(VideoFormat(
                quality=f"{quality_label} (progressive)",
                resolution=resolution_str,
                url=url,
                size_bytes=fmt.get('filesize') or fmt.get('filesize_approx'),
                format_id=format_id
            ))
        
        elif is_video_only:
            # HLS video-only stream
            quality_label = f"{height}p"
            resolution_str = f"{width}x{height}" if width and height else str(resolution)
            
            quality_key = f"{height}_hls"
            if quality_key in seen_video_qualities:
                continue
            seen_video_qualities.add(quality_key)
            
            video_formats.append(VideoFormat(
                quality=f"{quality_label} (hls)",
                resolution=resolution_str,
                url=url,
                size_bytes=fmt.get('filesize') or fmt.get('filesize_approx'),
                format_id=format_id
            ))
    
    # Sort by quality (height) descending
    def get_height(fmt):
        try:
            return int(fmt.quality.split('p')[0]) if 'p' in fmt.quality else 0
        except:
            return 0
    
    # Combine: progressive first (usually better for direct download), then HLS
    progressive_formats.sort(key=get_height, reverse=True)
    video_formats.sort(key=get_height, reverse=True)
    
    # Final video list: progressive + hls video
    all_videos = progressive_formats + video_formats
    
    # Sort audio by bitrate
    def get_bitrate(fmt):
        try:
            return int(fmt.quality.replace('kbps', ''))
        except:
            return 0
    
    audio_formats.sort(key=get_bitrate, reverse=True)
    
    # Sort images: orig first, then by resolution
    def get_image_priority(fmt):
        # Priority: ORIG > LARGE > others
        priority_map = {'orig': 0, 'large': 1, 'medium': 2, 'small': 3, 'thumb': 4}
        return priority_map.get(fmt.quality.lower(), 5)
    
    image_formats.sort(key=get_image_priority)
    
    return all_videos, audio_formats, image_formats


def extract_video_info(url: str) -> Dict[str, Any]:
    """
    Extract video info using yt-dlp (blocking operation)
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'socket_timeout': 30,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        logger.error(f"yt-dlp extraction failed for {url}: {e}")
        raise


def detect_platform(url: str, extractor: str) -> str:
    """Detect platform from URL and extractor"""
    url_lower = url.lower()
    extractor_lower = extractor.lower()
    
    if 'tiktok.com' in url_lower or 'douyin.com' in url_lower:
        return 'tiktok'
    elif 'twitter.com' in url_lower or 'x.com' in url_lower or 'twitter' in extractor_lower:
        return 'x'
    return 'unknown'


def parse_playlist_entries(entries: List[Dict]) -> List[MediaEntry]:
    """Parse playlist entries (multiple photos/videos in one tweet)"""
    parsed_entries = []
    
    for idx, entry in enumerate(entries):
        video_formats, audio_formats, image_formats = parse_formats(entry.get('formats', []))
        
        # Determine media type
        if image_formats and not video_formats:
            media_type = "photo"
            best_url = image_formats[0].url if image_formats else None
            formats = image_formats
        elif video_formats:
            media_type = "video"
            best_url = video_formats[0].url if video_formats else None
            formats = video_formats
        elif audio_formats:
            media_type = "audio"
            best_url = audio_formats[0].url if audio_formats else None
            formats = audio_formats
        else:
            media_type = "unknown"
            best_url = None
            formats = []
        
        duration = entry.get('duration')
        
        # Get thumbnail from entry
        thumbnails = entry.get('thumbnails', [])
        thumbnail = entry.get('thumbnail', '')
        if thumbnails and not thumbnail:
            thumbnail = thumbnails[0].get('url', '')
        
        parsed_entries.append(MediaEntry(
            entry_id=entry.get('id', f'entry_{idx}'),
            title=entry.get('title') or entry.get('fulltitle'),
            thumbnail=thumbnail,
            width=entry.get('width'),
            height=entry.get('height'),
            duration_seconds=duration,
            duration_formatted=format_duration(duration),
            media_type=media_type,
            formats=formats,
            best_url=best_url
        ))
    
    return parsed_entries


def build_response(info: Dict, original_url: str) -> DownloadResponse:
    """Build API response from yt-dlp info"""
    
    # Detect platform
    platform = detect_platform(original_url, info.get('extractor', ''))
    
    # Check if this is a playlist (multiple media entries)
    is_playlist = info.get('_type') == 'playlist'
    entries = info.get('entries', [])
    
    if is_playlist and entries:
        # Multiple media entries (e.g., multiple photos)
        parsed_entries = parse_playlist_entries(entries)
        
        # Determine content type based on entries
        content_types = set(e.media_type for e in parsed_entries)
        if content_types == {'photo'}:
            content_type = "photo"
            message = f"Photo gallery extracted successfully ({len(parsed_entries)} images)"
        elif 'photo' in content_types and 'video' in content_types:
            content_type = "mixed"
            message = f"Mixed media extracted successfully ({len(parsed_entries)} items)"
        else:
            content_type = "playlist"
            message = f"Playlist extracted successfully ({len(parsed_entries)} items)"
        
        # Use first entry's formats for backward compatibility
        first_entry = parsed_entries[0] if parsed_entries else None
        # Determine format lists based on first entry's media type
        if first_entry and first_entry.media_type == 'photo':
            video_formats = []
            audio_formats = []
            image_formats = first_entry.formats
        elif first_entry and first_entry.media_type == 'video':
            video_formats = first_entry.formats
            audio_formats = []
            image_formats = []
        else:
            video_formats = []
            audio_formats = []
            image_formats = []
        
        best_video_url = video_formats[0].url if video_formats else None
        best_audio_url = None
        best_image_url = image_formats[0].url if image_formats else (
            first_entry.best_url if first_entry and first_entry.media_type == 'photo' else None
        )
        
        # Get thumbnail from first entry
        thumbnail = first_entry.thumbnail if first_entry else ''
        
        # Get author info from main info
        author_name = info.get('uploader')
        author_username = info.get('uploader_id')
        
        # Get stats from main info
        stats = {
            'views': info.get('view_count'),
            'likes': info.get('like_count'),
            'comments': info.get('comment_count'),
            'shares': info.get('repost_count'),
        }
        
        # Parse upload date
        upload_date = info.get('upload_date')
        created_at = None
        if upload_date and len(upload_date) == 8:
            created_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        
        video_data = VideoData(
            platform=platform,
            content_type=content_type,
            video_id=info.get('id', ''),
            title=info.get('title') or info.get('fulltitle'),
            description=info.get('description'),
            author_name=author_name,
            author_username=author_username,
            author_avatar='',  # Playlist doesn't have single avatar
            thumbnail=thumbnail,
            duration_seconds=None,
            duration_formatted=None,
            stats={k: v for k, v in stats.items() if v is not None},
            created_at=created_at,
            original_url=original_url,
            is_playlist=True,
            playlist_count=len(parsed_entries),
            entries=parsed_entries
        )
        
        return DownloadResponse(
            success=True,
            message=message,
            data=video_data,
            video_formats=video_formats,
            audio_formats=audio_formats,
            image_formats=image_formats,
            best_video_url=best_video_url,
            best_audio_url=best_audio_url,
            best_image_url=best_image_url,
            extracted_at=datetime.utcnow().isoformat() + 'Z'
        )
    
    # Single media (video or photo)
    video_formats, audio_formats, image_formats = parse_formats(info.get('formats', []))
    
    # Determine content type
    if image_formats and not video_formats:
        content_type = "photo"
        best_url = image_formats[0].url if image_formats else None
        message = "Photo extracted successfully"
    elif video_formats:
        content_type = "video"
        best_url = video_formats[0].url if video_formats else None
        message = "Video info extracted successfully"
    elif audio_formats:
        content_type = "audio"
        best_url = audio_formats[0].url if audio_formats else None
        message = "Audio extracted successfully"
    else:
        content_type = "unknown"
        best_url = None
        message = "Media extracted successfully"
    
    # Get best URLs
    best_video_url = video_formats[0].url if video_formats else None
    best_audio_url = audio_formats[0].url if audio_formats else None
    best_image_url = image_formats[0].url if image_formats else best_url
    
    # Get thumbnail (prefer larger size)
    thumbnails = info.get('thumbnails', [])
    thumbnail = info.get('thumbnail', '')
    if thumbnails:
        # Try to find largest thumbnail
        largest = max(thumbnails, key=lambda t: t.get('width', 0) * t.get('height', 0), default=None)
        if largest:
            thumbnail = largest.get('url', thumbnail)
    
    # Get author avatar
    author_avatar = ''
    if thumbnails and len(thumbnails) > 0:
        author_avatar = thumbnails[0].get('url', '')
    
    # Parse upload date
    upload_date = info.get('upload_date')
    created_at = None
    if upload_date and len(upload_date) == 8:
        # Format: YYYYMMDD
        created_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    
    # Build stats
    stats = {
        'views': info.get('view_count'),
        'likes': info.get('like_count'),
        'comments': info.get('comment_count'),
        'shares': info.get('repost_count'),
    }
    
    # Build video data
    duration = info.get('duration')
    video_data = VideoData(
        platform=platform,
        content_type=content_type,
        video_id=info.get('id', ''),
        title=info.get('title') or info.get('fulltitle'),
        description=info.get('description'),
        author_name=info.get('uploader'),
        author_username=info.get('uploader_id'),
        author_avatar=author_avatar,
        thumbnail=thumbnail,
        duration_seconds=duration,
        duration_formatted=format_duration(duration),
        stats={k: v for k, v in stats.items() if v is not None},
        created_at=created_at,
        original_url=original_url,
        is_playlist=False,
        playlist_count=None,
        entries=[]
    )
    
    return DownloadResponse(
        success=True,
        message=message,
        data=video_data,
        video_formats=video_formats,
        audio_formats=audio_formats,
        image_formats=image_formats,
        best_video_url=best_video_url,
        best_audio_url=best_audio_url,
        best_image_url=best_image_url,
        extracted_at=datetime.utcnow().isoformat() + 'Z'
    )


# ============= API Endpoints =============

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "TikTok/X Video Downloader API",
        "version": "1.1.0",
        "endpoints": {
            "POST /download": "Extract video/photo info - body: {\"url\": \"media_url\"}",
            "GET /health": "Health check"
        },
        "supported_platforms": ["TikTok", "X (Twitter)"],
        "supported_media_types": ["Videos", "Photos/Galleries", "Mixed media posts"]
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + 'Z',
        "version": "1.1.0"
    }


@app.post("/download", response_model=DownloadResponse)
async def download_video(request: DownloadRequest):
    """
    Extract video/photo information and return download links
    
    Request body:
        - url: TikTok or X (Twitter) video/photo URL
    
    Returns:
        - Media metadata (title, author, stats, etc.)
        - List of available video formats with direct URLs
        - List of available audio formats with direct URLs
        - List of available image formats for photo posts
        - Best quality URLs for quick access
    """
    url = request.url.strip() if request.url else None
    
    # Validate URL
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    # Check if URL is supported
    supported_domains = ['tiktok.com', 'douyin.com', 'twitter.com', 'x.com']
    if not any(domain in url.lower() for domain in supported_domains):
        raise HTTPException(
            status_code=400, 
            detail="Unsupported URL. Only TikTok and X (Twitter) URLs are supported."
        )
    
    try:
        # Extract info using yt-dlp (run in thread pool)
        loop = asyncio.get_event_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(executor, extract_video_info, url),
            timeout=45.0
        )
        
        if not info:
            raise HTTPException(status_code=500, detail="Failed to extract video info")
        
        # Build response
        response = build_response(info, url)
        return response
        
    except asyncio.TimeoutError:
        logger.error(f"Timeout extracting video info for: {url[:50]}...")
        raise HTTPException(status_code=504, detail="Request timeout - video extraction took too long")
        
    except Exception as e:
        error_str = str(e)
        logger.error(f"Error extracting video: {error_str}")
        
        # Map common errors to appropriate status codes
        error_lower = error_str.lower()
        
        if 'unsupported url' in error_lower:
            raise HTTPException(status_code=400, detail="Unsupported or invalid URL")
        
        elif 'no video or photo could be found' in error_lower or 'no video' in error_lower or 'no photo' in error_lower:
            raise HTTPException(
                status_code=404, 
                detail="No video or photo found in this post. The URL may be a text-only post."
            )
        
        elif 'not found' in error_lower or 'unable to download' in error_lower:
            raise HTTPException(status_code=404, detail="Video not found or may be private/deleted")
        
        elif '403' in error_str or 'forbidden' in error_lower:
            raise HTTPException(status_code=403, detail="Access forbidden - video may be private or region-restricted")
        
        elif 'login' in error_lower or 'authentication' in error_lower:
            raise HTTPException(status_code=401, detail="This content requires login/authentication")
        
        else:
            raise HTTPException(status_code=500, detail=f"Extraction failed: {error_str}")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": exc.detail,
            "error_code": f"HTTP_{exc.status_code}"
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Internal server error",
            "error_code": "INTERNAL_ERROR"
        }
    )


# ============= Main =============

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv('PORT', 8000))
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info"
    )
