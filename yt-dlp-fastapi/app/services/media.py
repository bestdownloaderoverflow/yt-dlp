from typing import Dict, Any

import yt_dlp

from app.config import YDL_BASE_OPTS
from app.utils.platform import detect_platform


def fetch_media_info(url: str) -> Dict[str, Any]:
    """
    Fetch media information without downloading.
    Auto-detects video or photo content.
    Supports: YouTube, TikTok, Twitter/X
    """
    ydl_opts = YDL_BASE_OPTS.copy()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
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
                    'platform': detect_platform(url),
                    'title': sanitized_info.get('title'),
                    'uploader': sanitized_info.get('uploader'),
                    'photo_count': len(photos),
                    'photos': photos,
                    'original_url': url,
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
                    'platform': detect_platform(url),
                    'title': sanitized_info.get('title'),
                    'uploader': sanitized_info.get('uploader'),
                    'video_count': len(videos),
                    'videos': videos,
                    'original_url': url,
                }
        
        # Single media (not playlist)
        formats = sanitized_info.get('formats', [])
        
        # Check if it's a photo (single image)
        image_formats = [f for f in formats if (f.get('video_ext') or '').lower() in ('jpg', 'jpeg', 'png', 'webp', 'gif')]
        if image_formats:
            return {
                'content_type': 'photo',
                'platform': detect_platform(url),
                'title': sanitized_info.get('title'),
                'uploader': sanitized_info.get('uploader'),
                'photos': [{
                    'index': 0,
                    'url': f.get('url'),
                    'width': f.get('width'),
                    'height': f.get('height'),
                } for f in image_formats],
                'original_url': url,
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
                'original_url': url,
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
            'platform': detect_platform(url),
            'title': sanitized_info.get('title'),
            'description': sanitized_info.get('description'),
            'duration': int(sanitized_info.get('duration')) if sanitized_info.get('duration') else None,
            'uploader': sanitized_info.get('uploader'),
            'thumbnail': sanitized_info.get('thumbnail'),
            'formats': video_formats,
            'original_url': url,
        }
