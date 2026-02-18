import subprocess
from typing import List
from pathlib import Path

import httpx


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
