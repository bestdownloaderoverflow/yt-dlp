"""Celery worker entry point."""
import os
import sys

# Add parent directory to path for local yt_dlp
sys.path.insert(0, '/Users/almafazi/Documents/yt-dlp-tiktok')

from app.celery_app import celery_app

# Import tasks to register them
from app.services import download

if __name__ == "__main__":
    celery_app.start()
