"""
yt-dlp Stream API - Entry point

Refactored modular architecture:
- api/: Endpoint routers
- core/: Shared utilities and generators
- services/: Business logic (ytdl_manager, process_manager, etc.)
"""

import logging
import os
import sys
from pathlib import Path

# Add parent directory to path for local yt_dlp (only needed for non-Docker runs)
# In Docker, PYTHONPATH is already set in Dockerfile
if not os.getenv("PYTHONPATH", "").startswith("/app"):
    sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI

from api import router as api_router

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ytdlp_stream")

# Create FastAPI app
app = FastAPI(title="yt-dlp Stream API", version="3.0.0")

# Include all API routes
app.include_router(api_router)


@app.get("/")
def root():
    """API root with endpoint documentation."""
    return {
        "message": "Its Work!",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
