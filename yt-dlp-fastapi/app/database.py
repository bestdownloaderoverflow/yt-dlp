"""MongoDB database connection and models."""
from datetime import datetime
from typing import Optional, List
from enum import Enum

from beanie import Document, Indexed, init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: str = "unknown"
    filesize: Optional[int] = None


class JobMetadata(BaseModel):
    title: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    platform: Optional[str] = None
    formats: List[FormatInfo] = Field(default_factory=list)


class DownloadJob(Document):
    """Download job document model."""

    job_id: Indexed(str, unique=True)
    url: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    error_message: Optional[str] = None
    format_id: Optional[str] = None
    download_mp3: bool = False

    # File info
    output_file: Optional[str] = None
    file_size: Optional[int] = None

    # Metadata
    metadata: JobMetadata = Field(default_factory=JobMetadata)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    # Celery task id
    celery_task_id: Optional[str] = None

    # Retry info
    retry_count: int = 0
    max_retries: int = 3

    class Settings:
        name = "download_jobs"
        indexes = [
            "status",
            "created_at",
        ]

    async def update_progress(self, progress: float, speed: Optional[str] = None, eta: Optional[str] = None):
        """Update job progress."""
        self.progress = progress
        if speed:
            self.speed = speed
        if eta:
            self.eta = eta
        self.updated_at = datetime.utcnow()
        await self.save()

    async def mark_completed(self, output_file: str, file_size: Optional[int] = None):
        """Mark job as completed."""
        self.status = JobStatus.COMPLETED
        self.progress = 100.0
        self.output_file = output_file
        self.file_size = file_size
        self.completed_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        await self.save()

    async def mark_failed(self, error_message: str):
        """Mark job as failed."""
        self.status = JobStatus.FAILED
        self.error_message = error_message
        self.updated_at = datetime.utcnow()
        await self.save()

    async def mark_downloading(self):
        """Mark job as downloading."""
        self.status = JobStatus.DOWNLOADING
        self.started_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        await self.save()


async def init_database(mongodb_url: str):
    """Initialize MongoDB connection and Beanie ODM."""
    client = AsyncIOMotorClient(mongodb_url)
    await init_beanie(
        database=client.get_default_database(),
        document_models=[DownloadJob]
    )
    return client
