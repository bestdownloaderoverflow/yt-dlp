"""Main FastAPI application."""
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Add parent directory to path for local yt_dlp
sys.path.insert(0, '/Users/almafazi/Documents/yt-dlp-tiktok')

from app.config import settings
from app.database import init_database
from app.utils.rate_limit import setup_rate_limiting
from app.routers import media, tiktok
from app.models import HealthResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    print(f"Starting server with cleanup delay: {settings.CLEANUP_DELAY_MINUTES}min")
    print(f"Connecting to MongoDB: {settings.MONGODB_URL}")

    # Initialize database
    try:
        app.state.db_client = await init_database(settings.MONGODB_URL)
        print("Database connected successfully")
    except Exception as e:
        print(f"Warning: Database connection failed: {e}")
        print("App will continue without database persistence")

    yield

    # Shutdown
    print("Shutting down server...")
    if hasattr(app.state, 'db_client'):
        app.state.db_client.close()
        print("Database connection closed")


app = FastAPI(
    title="YouTube Downloader API",
    version="1.1.0",
    description="Production-ready video downloader with Celery workers",
    lifespan=lifespan
)

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup rate limiting
setup_rate_limiting(app)

# Include routers
app.include_router(media.router, prefix="/api/v1")
app.include_router(tiktok.router, prefix="/api/v1")


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    # Check Database
    db_status = "disconnected"
    try:
        from app.database import DownloadJob
        await DownloadJob.find_one()
        db_status = "connected"
    except:
        pass

    # Check Redis
    redis_status = "disconnected"
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        await r.ping()
        await r.close()
        redis_status = "connected"
    except:
        pass

    # Check RabbitMQ via Celery
    broker_status = "disconnected"
    try:
        from app.celery_app import celery_app
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1)
        broker_status = "connected"
    except:
        pass

    # Overall status
    overall_status = "healthy" if db_status == "connected" else "degraded"

    return HealthResponse(
        status=overall_status,
        database=db_status,
        broker=broker_status,
        redis=redis_status
    )


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "YouTube Downloader API",
        "version": "1.1.0",
        "docs": "/docs",
        "health": "/health"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
