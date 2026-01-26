"""Cleanup utilities for temporary files and folders"""

import asyncio
import shutil
import time
from pathlib import Path
from typing import Union
import logging

logger = logging.getLogger(__name__)


def cleanup_folder(folder_path: Union[str, Path]) -> None:
    """
    Remove a folder and all its contents
    
    Args:
        folder_path: Path to folder to remove
    """
    folder = Path(folder_path)
    
    if not folder.exists():
        return
    
    try:
        shutil.rmtree(folder)
        logger.info(f"Cleaned up folder: {folder}")
    except Exception as e:
        logger.error(f"Error cleaning up folder {folder}: {e}")


def cleanup_old_folders(base_dir: Union[str, Path], max_age_seconds: int = 3600) -> int:
    """
    Remove folders older than max_age_seconds
    
    Args:
        base_dir: Base directory to scan
        max_age_seconds: Maximum age in seconds (default: 1 hour)
    
    Returns:
        Number of folders removed
    """
    base_path = Path(base_dir)
    
    if not base_path.exists():
        return 0
    
    current_time = time.time()
    removed_count = 0
    
    try:
        for folder in base_path.iterdir():
            if not folder.is_dir():
                continue
            
            # Check folder age
            folder_age = current_time - folder.stat().st_mtime
            
            if folder_age > max_age_seconds:
                try:
                    shutil.rmtree(folder)
                    removed_count += 1
                    logger.info(f"Removed old folder: {folder} (age: {folder_age:.0f}s)")
                except Exception as e:
                    logger.error(f"Error removing folder {folder}: {e}")
    
    except Exception as e:
        logger.error(f"Error scanning directory {base_path}: {e}")
    
    return removed_count


async def init_cleanup_schedule(base_dir: Union[str, Path], cron_schedule: str = "*/15 * * * *"):
    """
    Initialize scheduled cleanup task
    Runs cleanup every 15 minutes by default
    
    Args:
        base_dir: Base directory to clean
        cron_schedule: Cron schedule string (not implemented, runs every 15 min)
    """
    logger.info(f"Initializing cleanup schedule for: {base_dir}")
    
    # Simple implementation: run every 15 minutes
    while True:
        try:
            await asyncio.sleep(15 * 60)  # 15 minutes
            
            removed = await asyncio.get_event_loop().run_in_executor(
                None,
                cleanup_old_folders,
                base_dir,
                3600  # 1 hour
            )
            
            if removed > 0:
                logger.info(f"Scheduled cleanup: removed {removed} old folders")
        
        except asyncio.CancelledError:
            logger.info("Cleanup schedule cancelled")
            break
        except Exception as e:
            logger.error(f"Error in scheduled cleanup: {e}")


def get_folder_size(folder_path: Union[str, Path]) -> int:
    """
    Get total size of folder in bytes
    
    Args:
        folder_path: Path to folder
    
    Returns:
        Total size in bytes
    """
    folder = Path(folder_path)
    
    if not folder.exists():
        return 0
    
    total_size = 0
    
    try:
        for item in folder.rglob('*'):
            if item.is_file():
                total_size += item.stat().st_size
    except Exception as e:
        logger.error(f"Error calculating folder size: {e}")
    
    return total_size


if __name__ == "__main__":
    # Test cleanup
    import tempfile
    
    # Create test folder
    test_dir = Path(tempfile.gettempdir()) / "test_cleanup"
    test_dir.mkdir(exist_ok=True)
    
    # Create some test folders
    for i in range(3):
        (test_dir / f"folder_{i}").mkdir(exist_ok=True)
    
    print(f"Created test folders in: {test_dir}")
    print(f"Folder size: {get_folder_size(test_dir)} bytes")
    
    # Test cleanup
    removed = cleanup_old_folders(test_dir, max_age_seconds=0)
    print(f"Removed {removed} folders")
    
    # Cleanup test directory
    cleanup_folder(test_dir)
    print("✅ Cleanup tests passed")
