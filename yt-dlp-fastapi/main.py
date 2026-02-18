from fastapi import FastAPI

from app.config import CLEANUP_DELAY_MINUTES
from app.routers import media, tiktok

app = FastAPI(title="YouTube Downloader API", version="1.0.0")

# Include routers
app.include_router(media.router)
app.include_router(tiktok.router)


@app.on_event("startup")
async def startup_event():
    """Server startup event"""
    print(f"Server started with per-job cleanup ({CLEANUP_DELAY_MINUTES}min delay)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
