"""Slideshow creation utilities using FFmpeg"""

import subprocess
import logging
from pathlib import Path
from typing import List, Union
import httpx

logger = logging.getLogger(__name__)


def download_file(url: str, output_path: Union[str, Path], timeout: int = 120) -> str:
    """
    Download file from URL to local path
    
    Args:
        url: URL to download
        output_path: Local path to save file
        timeout: Request timeout in seconds
    
    Returns:
        Path to downloaded file
    """
    output = Path(output_path)
    
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream('GET', url) as response:
                response.raise_for_status()
                
                with open(output, 'wb') as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
        
        logger.info(f"Downloaded file: {output}")
        return str(output)
    
    except Exception as e:
        # Clean up partial file
        if output.exists():
            output.unlink()
        raise Exception(f"Failed to download file: {e}")


def create_slideshow(
    image_paths: List[str],
    audio_path: str,
    output_path: str,
    duration_per_image: int = 4
) -> None:
    """
    Create a slideshow video from images and audio using FFmpeg
    
    Args:
        image_paths: List of image file paths
        audio_path: Path to audio file
        output_path: Path for output video
        duration_per_image: Duration per image in seconds (default: 4)
    """
    if not image_paths:
        raise ValueError("No image paths provided")
    
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    for img_path in image_paths:
        if not Path(img_path).exists():
            raise FileNotFoundError(f"Image file not found: {img_path}")
    
    try:
        # Build FFmpeg command
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
        
        logger.info(f"Creating slideshow with {len(image_paths)} images")
        
        # Run FFmpeg
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )
        
        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            raise Exception(f"FFmpeg failed with code {result.returncode}")
        
        if not Path(output_path).exists():
            raise Exception("Output file was not created")
        
        logger.info(f"Slideshow created successfully: {output_path}")
    
    except subprocess.TimeoutExpired:
        raise Exception("Slideshow creation timeout after 5 minutes")
    except Exception as e:
        logger.error(f"Error creating slideshow: {e}")
        # Clean up output file if it exists
        output = Path(output_path)
        if output.exists():
            output.unlink()
        raise


def test_slideshow():
    """Test slideshow creation"""
    import tempfile
    from PIL import Image
    import io
    
    # Create temporary directory
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        # Create test images
        image_paths = []
        for i in range(3):
            img = Image.new('RGB', (1080, 1920), color=(255, i * 80, 0))
            img_path = temp_dir / f'test_image_{i}.jpg'
            img.save(img_path)
            image_paths.append(str(img_path))
        
        # Create dummy audio (silent)
        audio_path = temp_dir / 'test_audio.mp3'
        # For actual test, you'd need a real audio file
        # audio_path.touch()
        
        output_path = temp_dir / 'test_output.mp4'
        
        print(f"Test files created in: {temp_dir}")
        print(f"Images: {len(image_paths)}")
        
        # Note: This will fail without a real audio file
        # create_slideshow(image_paths, str(audio_path), str(output_path))
        
        print("✅ Slideshow module loaded successfully")
    
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    test_slideshow()
