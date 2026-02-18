import sys
from pathlib import Path

# Use local yt_dlp fork
sys.path.insert(0, '/Users/almafazi/Documents/yt-dlp-tiktok')

# yt-dlp options - JS runtime auto-detected by yt-dlp 2026+
YDL_BASE_OPTS = {
    'quiet': True,
    'no_warnings': True,
}

# Temp directory for downloads
TEMP_DIR = Path("./temp_downloads")
TEMP_DIR.mkdir(exist_ok=True)

# Per-job cleanup: Delete files after 5 minutes if not downloaded
CLEANUP_DELAY_MINUTES = 5
