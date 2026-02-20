import sys
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings

# Use local yt_dlp fork (for development)
sys.path.insert(0, '/Users/almafazi/Documents/yt-dlp-tiktok')


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    MONGODB_URL: str = "mongodb://localhost:27017/yt_dlp_db"

    # Message Broker
    CELERY_BROKER_URL: str = "amqp://guest:guest@localhost:5672/"
    CELERY_RESULT_BACKEND: str = "mongodb://localhost:27017/yt_dlp_celery_results"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # App Settings
    CLEANUP_DELAY_MINUTES: int = 5
    RATE_LIMIT_PER_MINUTE: int = 10
    LOG_LEVEL: str = "info"

    # File Storage
    TEMP_DIR: Path = Path("./temp_downloads")

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()

# Ensure temp directory exists
settings.TEMP_DIR.mkdir(exist_ok=True)

# yt-dlp options
YDL_BASE_OPTS = {
    'quiet': True,
    'no_warnings': True,
}

# For backward compatibility
TEMP_DIR = settings.TEMP_DIR
CLEANUP_DELAY_MINUTES = settings.CLEANUP_DELAY_MINUTES
MONGODB_URL = settings.MONGODB_URL
CELERY_BROKER_URL = settings.CELERY_BROKER_URL
REDIS_URL = settings.REDIS_URL
