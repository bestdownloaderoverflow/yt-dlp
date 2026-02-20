"""Celery application configuration."""
import os
from celery import Celery
from celery.signals import task_prerun, task_postrun, task_failure

from app.config import settings

# Celery configuration
celery_app = Celery(
    "yt_dlp_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.services.download"]
)

celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Default queue
    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",

    # Task execution
    task_track_started=True,
    task_time_limit=3600,  # 1 hour max per task
    task_soft_time_limit=3300,  # Soft limit 55 minutes

    # Worker settings
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=50,

    # Result backend
    result_expires=3600 * 24 * 7,  # Results expire after 7 days
    result_backend_always_retry=True,
    result_backend_max_retries=10,

    # Retry settings
    task_default_retry_delay=60,  # 1 minute
    task_max_retries=3,

    # Queue configuration - route download tasks to download queue
    task_routes={
        "app.services.download.download_video_task": {"queue": "download"},
    },

    # Beat schedule for periodic tasks
    beat_schedule={
        "cleanup-expired-jobs": {
            "task": "app.services.download.cleanup_expired_jobs",
            "schedule": 300.0,  # Every 5 minutes
        },
        "cleanup-old-files": {
            "task": "app.services.download.cleanup_old_files",
            "schedule": 600.0,  # Every 10 minutes
        },
    },
)


@task_prerun.connect
def task_prerun_handler(task_id, task, args, kwargs, **extras):
    """Handle task pre-run events."""
    print(f"Task {task.name}[{task_id}] started")


@task_postrun.connect
def task_postrun_handler(task_id, task, args, kwargs, retval, state, **extras):
    """Handle task post-run events."""
    print(f"Task {task.name}[{task_id}] finished with state: {state}")


@task_failure.connect
def task_failure_handler(task_id, exception, args, kwargs, traceback, einfo, **extras):
    """Handle task failure events."""
    print(f"Task failed: {task_id}, Exception: {exception}")
