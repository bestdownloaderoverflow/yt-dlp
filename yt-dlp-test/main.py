#!/usr/bin/env python3
"""
Simple yt-dlp wrapper to extract and print JSON info from a URL.
"""
import sys
import json
from pathlib import Path

# Add parent directory to path to import local yt_dlp
sys.path.insert(0, str(Path(__file__).parent.parent))

from yt_dlp import YoutubeDL


def extract_info(url):
    """Extract info from URL and return as JSON."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <url>", file=sys.stderr)
        sys.exit(1)
    
    url = sys.argv[1]
    
    try:
        info = extract_info(url)
        print(json.dumps(info, indent=2, default=str))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
