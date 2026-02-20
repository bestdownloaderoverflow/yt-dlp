from typing import Optional, Union, List
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, HttpUrl, Field


class VideoRequest(BaseModel):
    url: HttpUrl
    format_id: Optional[str] = None
    download_mp3: bool = False


class TikTokRequest(BaseModel):
    url: HttpUrl


class ProcessResponse(BaseModel):
    job_id: str
    title: Optional[str] = None
    duration: Optional[Union[int, float]] = None
    thumbnail: Optional[str] = None
    formats: list = []
    status: str = "queued"


class ProgressResponse(BaseModel):
    job_id: str
    status: str
    progress: float
    speed: Optional[str] = None
    eta: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    output_file: Optional[str] = None


# New models for job management

class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"


class JobListItem(BaseModel):
    job_id: str
    url: str
    status: JobStatus
    progress: float
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None


class JobListResponse(BaseModel):
    jobs: List[JobListItem]
    total: int
    page: int = 1
    page_size: int = 20


class JobDetailResponse(BaseModel):
    job_id: str
    url: str
    status: JobStatus
    progress: float
    speed: Optional[str] = None
    eta: Optional[str] = None
    error_message: Optional[str] = None
    format_id: Optional[str] = None
    download_mp3: bool = False
    output_file: Optional[str] = None
    file_size: Optional[int] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int = 0


class RetryResponse(BaseModel):
    job_id: str
    status: str
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str = "1.0.0"
    database: str
    broker: str
    redis: str
