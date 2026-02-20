"""Celery tasks for video download."""
import os
import asyncio
from typing import Optional
from pathlib import Path
from datetime import datetime, timedelta

import yt_dlp
from celery import shared_task, Task
from celery.exceptions import SoftTimeLimitExceeded

from app.config import YDL_BASE_OPTS, TEMP_DIR, CLEANUP_DELAY_MINUTES
from app.database import DownloadJob, JobStatus, JobMetadata, FormatInfo


class CallbackTask(Task):
    """Base task class with callbacks."""
    def on_success(self, retval, task_id, args, kwargs):
        print(f"Task {task_id} succeeded")

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        print(f"Task {task_id} failed: {exc}")


def progress_hook(job_id: str, job_ref: dict):
    """Create progress hook for yt-dlp - stores progress in shared dict."""
    last_update = [0]

    def hook(d):
        import time
        current_time = time.time()
        if current_time - last_update[0] < 2:  # Update every 2 seconds
            return
        last_update[0] = current_time

        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            speed = d.get('_speed_str') or d.get('speed_string') or 'N/A'
            eta = d.get('_eta_str') or d.get('eta_string') or 'N/A'
            progress = (downloaded / total * 100) if total > 0 else 0

            # Store in shared dict
            job_ref['progress'] = round(progress, 2)
            job_ref['speed'] = speed
            job_ref['eta'] = eta
            job_ref['updated'] = True
            print(f"Progress: {progress:.1f}% | Speed: {speed} | ETA: {eta}")

        elif d['status'] == 'finished':
            print(f"Download finished for job {job_id}")

    return hook


@shared_task(bind=True, base=CallbackTask, max_retries=3, default_retry_delay=60)
def download_video_task(self, job_id: str, url: str, format_id: Optional[str], download_mp3: bool):
    """Celery task to download video."""
    import motor.motor_asyncio
    from app.config import settings

    # Create new event loop for async operations
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_download():
        # Connect to database
        client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URL)
        from beanie import init_beanie
        await init_beanie(
            database=client.get_default_database(),
            document_models=[DownloadJob]
        )

        # Get job
        job = await DownloadJob.find_one(DownloadJob.job_id == job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Update job status
        await job.mark_downloading()
        job.celery_task_id = self.request.id
        await job.save()

        download_dir = TEMP_DIR / job_id
        download_dir.mkdir(exist_ok=True)

        # Shared dict for progress updates
        job_ref = {'progress': 0.0, 'speed': None, 'eta': None, 'updated': False}

        try:
            ydl_opts = YDL_BASE_OPTS.copy()
            ydl_opts.update({
                'outtmpl': str(download_dir / '%(title)s.%(ext)s'),
                'progress_hooks': [progress_hook(job_id, job_ref)],
                'noprogress': False,
                'verbose': False,
            })

            if download_mp3:
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
            elif format_id:
                ydl_opts['format'] = format_id
            else:
                ydl_opts['format'] = 'best'

            # Run download in executor to not block
            def run_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            # Run with periodic progress sync
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_download)

                # Sync progress while download is running
                while not future.done():
                    if job_ref.get('updated'):
                        job.progress = job_ref['progress']
                        job.speed = job_ref['speed']
                        job.eta = job_ref['eta']
                        job.updated_at = datetime.utcnow()
                        await job.save()
                        job_ref['updated'] = False
                    await asyncio.sleep(1)

                # Wait for completion
                future.result()

            # Find downloaded file
            files = list(download_dir.iterdir())
            if files:
                output_file = files[0]
                file_size = output_file.stat().st_size if output_file.exists() else None

                # Update job as completed with expiry
                job.status = JobStatus.COMPLETED
                job.progress = 100.0
                job.output_file = str(output_file)
                job.file_size = file_size
                job.completed_at = datetime.utcnow()
                job.updated_at = datetime.utcnow()
                job.expires_at = datetime.utcnow() + timedelta(minutes=CLEANUP_DELAY_MINUTES)
                await job.save()

                return {
                    'job_id': job_id,
                    'status': 'completed',
                    'output_file': str(output_file),
                    'file_size': file_size
                }
            else:
                raise Exception('No file found after download')

        except SoftTimeLimitExceeded:
            await job.mark_failed('Download timed out')
            raise self.retry(exc=Exception('Download timed out'))

        except Exception as exc:
            await job.mark_failed(str(exc))
            if job.retry_count < job.max_retries:
                job.retry_count += 1
                await job.save()
                raise self.retry(exc=exc)
            raise

        finally:
            client.close()

    return loop.run_until_complete(run_download())


@shared_task
def cleanup_expired_jobs():
    """Periodic task to cleanup expired jobs."""
    import motor.motor_asyncio
    from app.config import settings

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_cleanup():
        client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URL)
        from beanie import init_beanie
        await init_beanie(
            database=client.get_default_database(),
            document_models=[DownloadJob]
        )

        now = datetime.utcnow()

        # Find expired completed jobs
        expired_jobs = await DownloadJob.find(
            DownloadJob.status == JobStatus.COMPLETED,
            DownloadJob.expires_at < now
        ).to_list()

        cleaned_count = 0
        for job in expired_jobs:
            try:
                if job.output_file and os.path.exists(job.output_file):
                    os.unlink(job.output_file)
                    print(f"Cleaned up file: {job.output_file}")

                # Clean up directory
                download_dir = TEMP_DIR / job.job_id
                if download_dir.exists():
                    import shutil
                    shutil.rmtree(download_dir)

                job.status = JobStatus.CLEANED
                await job.save()
                cleaned_count += 1

            except Exception as e:
                print(f"Failed to cleanup job {job.job_id}: {e}")

        client.close()
        return {'cleaned_jobs': cleaned_count}

    return loop.run_until_complete(run_cleanup())


@shared_task
def cleanup_old_files():
    """Cleanup old files that might have been missed."""
    import shutil

    cleaned = 0
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=CLEANUP_DELAY_MINUTES * 2)

    if not TEMP_DIR.exists():
        return {'cleaned_files': cleaned}

    for item in TEMP_DIR.iterdir():
        try:
            stat = item.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)

            if mtime < cutoff:
                if item.is_file():
                    item.unlink()
                    cleaned += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    cleaned += 1

        except Exception as e:
            print(f"Failed to cleanup {item}: {e}")

    return {'cleaned_files': cleaned}
