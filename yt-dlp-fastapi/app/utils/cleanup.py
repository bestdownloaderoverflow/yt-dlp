import asyncio
import shutil
from pathlib import Path

from app.config import TEMP_DIR, CLEANUP_DELAY_MINUTES
from app.services.download import progress_store


async def scheduled_cleanup(path: Path, is_file: bool = False):
    """Schedule cleanup for a specific path after delay"""
    await asyncio.sleep(CLEANUP_DELAY_MINUTES * 60)
    try:
        if is_file and path.exists():
            path.unlink()
            print(f"Auto cleanup: deleted file {path.name}")
        elif not is_file and path.exists():
            shutil.rmtree(path)
            print(f"Auto cleanup: deleted directory {path.name}")
    except Exception as e:
        print(f"Scheduled cleanup error for {path}: {e}")


async def cleanup_file(download_id: str, file_path: Path):
    """Clean up temporary files immediately after download completes."""
    await asyncio.sleep(5)  # Wait for file to be sent

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

        print(f"Immediate cleanup completed for {download_id}")

    except Exception as e:
        print(f"Cleanup error for {download_id}: {e}")
