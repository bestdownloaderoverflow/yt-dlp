import os
import uuid
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse

import yt_dlp

from app.config import YDL_BASE_OPTS, TEMP_DIR, settings
from app.database import DownloadJob, JobStatus, JobMetadata, FormatInfo
from app.models import (
    VideoRequest, ProcessResponse, ProgressResponse,
    JobListResponse, JobListItem, JobDetailResponse, RetryResponse
)
from app.services.download import download_video_task
from app.services.media import fetch_media_info

router = APIRouter()


@router.get("/")
async def root():
    return {"message": "YouTube Downloader API", "docs": "/docs"}


@router.post("/fetch")
async def fetch_media(request: VideoRequest):
    """
    Fetch media information without downloading.
    Auto-detects video or photo content.
    Supports: YouTube, TikTok, Twitter/X
    """
    try:
        return fetch_media_info(str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch media info: {str(e)}")


@router.post("/process", response_model=ProcessResponse)
async def process_video(request: VideoRequest):
    """
    Start processing/downloading a video.
    Returns a job_id to track progress.
    """
    job_id = str(uuid.uuid4())

    try:
        # First, get video info
        ydl_opts = YDL_BASE_OPTS.copy()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(str(request.url), download=False)
            sanitized_info = ydl.sanitize_info(info)

        # Create formats list
        formats = [
            FormatInfo(
                format_id=f.get('format_id'),
                ext=f.get('ext'),
                resolution=f.get('resolution', 'unknown'),
                filesize=f.get('filesize')
            )
            for f in sanitized_info.get('formats', [])
        ]

        # Create job metadata
        metadata = JobMetadata(
            title=sanitized_info.get('title'),
            duration=sanitized_info.get('duration'),
            thumbnail=sanitized_info.get('thumbnail'),
            uploader=sanitized_info.get('uploader'),
            platform=sanitized_info.get('extractor'),
            formats=formats
        )

        # Create job in database
        job = DownloadJob(
            job_id=job_id,
            url=str(request.url),
            status=JobStatus.QUEUED,
            format_id=request.format_id,
            download_mp3=request.download_mp3,
            metadata=metadata
        )
        await job.insert()

        # Submit to Celery
        task = download_video_task.delay(
            job_id=job_id,
            url=str(request.url),
            format_id=request.format_id,
            download_mp3=request.download_mp3
        )

        # Update job with celery task id
        job.celery_task_id = task.id
        await job.save()

        return ProcessResponse(
            job_id=job_id,
            title=metadata.title,
            duration=metadata.duration,
            thumbnail=metadata.thumbnail,
            formats=[f.model_dump() for f in formats],
            status="queued"
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to process video: {str(e)}")


@router.get("/check-progress/{job_id}", response_model=ProgressResponse)
async def check_progress(job_id: str):
    """
    Check the download progress of a job.
    """
    job = await DownloadJob.find_one(DownloadJob.job_id == job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found")

    return ProgressResponse(
        job_id=job.job_id,
        status=job.status.value,
        progress=job.progress,
        speed=job.speed,
        eta=job.eta,
        filename=job.metadata.title if job.metadata else None,
        error=job.error_message,
        output_file=job.output_file
    )


@router.get("/download/{job_id}")
async def download_file(job_id: str, background_tasks: BackgroundTasks):
    """
    Download the processed video file.
    File will be deleted after configured delay.
    """
    job = await DownloadJob.find_one(DownloadJob.job_id == job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found")

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Download not yet completed")

    if not job.output_file or not os.path.exists(job.output_file):
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(job.output_file)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type='application/octet-stream'
    )


@router.delete("/cleanup/{job_id}")
async def manual_cleanup(job_id: str):
    """
    Manually cleanup a job entry and its files.
    """
    job = await DownloadJob.find_one(DownloadJob.job_id == job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found")

    try:
        if job.output_file and os.path.exists(job.output_file):
            Path(job.output_file).unlink()

        download_dir = TEMP_DIR / job_id
        if download_dir.exists():
            shutil.rmtree(download_dir)

        job.status = JobStatus.CLEANED
        await job.save()

        return {"message": "Cleanup successful"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: Optional[JobStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List all jobs with optional filtering.
    """
    query = DownloadJob.find()

    if status:
        query = query.find(DownloadJob.status == status)

    # Get total count
    total = await query.count()

    # Get paginated results
    jobs = await query.skip((page - 1) * page_size).limit(page_size).to_list()

    return JobListResponse(
        jobs=[
            JobListItem(
                job_id=j.job_id,
                url=j.url,
                status=j.status,
                progress=j.progress,
                created_at=j.created_at,
                updated_at=j.updated_at,
                completed_at=j.completed_at,
                error_message=j.error_message
            )
            for j in jobs
        ],
        total=total,
        page=page,
        page_size=page_size
    )


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job_detail(job_id: str):
    """
    Get detailed information about a specific job.
    """
    job = await DownloadJob.find_one(DownloadJob.job_id == job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found")

    return JobDetailResponse(
        job_id=job.job_id,
        url=job.url,
        status=job.status,
        progress=job.progress,
        speed=job.speed,
        eta=job.eta,
        error_message=job.error_message,
        format_id=job.format_id,
        download_mp3=job.download_mp3,
        output_file=job.output_file,
        file_size=job.file_size,
        metadata=job.metadata.model_dump() if job.metadata else {},
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        retry_count=job.retry_count
    )


@router.post("/jobs/{job_id}/retry", response_model=RetryResponse)
async def retry_job(job_id: str):
    """
    Retry a failed job.
    """
    job = await DownloadJob.find_one(DownloadJob.job_id == job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found")

    if job.status not in [JobStatus.FAILED, JobStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Only failed or cancelled jobs can be retried")

    # Reset job status
    job.status = JobStatus.QUEUED
    job.progress = 0.0
    job.error_message = None
    job.retry_count += 1
    await job.save()

    # Submit to Celery again
    task = download_video_task.delay(
        job_id=job_id,
        url=job.url,
        format_id=job.format_id,
        download_mp3=job.download_mp3
    )

    job.celery_task_id = task.id
    await job.save()

    return RetryResponse(
        job_id=job_id,
        status="queued",
        message="Job has been queued for retry"
    )


@router.get("/active-downloads")
async def list_active_downloads():
    """
    List all active downloads.
    """
    active_jobs = await DownloadJob.find(
        DownloadJob.status.in_([JobStatus.PENDING, JobStatus.QUEUED, JobStatus.DOWNLOADING])
    ).to_list()

    return {
        "active_downloads": [
            {
                "job_id": job.job_id,
                "status": job.status.value,
                "progress": job.progress
            }
            for job in active_jobs
        ]
    }
