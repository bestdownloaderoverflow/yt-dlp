"""
Gunicorn configuration for TikTok Downloader Server
Optimized for 6 CPU cores and high concurrency (1000+ users)
"""

import multiprocessing
import os

# Server socket
bind = "0.0.0.0:3021"
backlog = 2048

# Worker processes
# Formula: (2 x CPU cores) + 1 = (2 x 6) + 1 = 13 workers
workers = 5
worker_class = "uvicorn.workers.UvicornWorker"

# Worker connections
# Each worker can handle this many concurrent connections
worker_connections = 1000

# Timeout settings
timeout = 120  # Match DOWNLOAD_TIMEOUT
graceful_timeout = 30
keepalive = 5

# Request limits
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# Logging
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
accesslog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = "tiktok-downloader"

# Preload app for faster worker spawning and memory sharing
preload_app = True

# Max requests per worker before restart (prevents memory leaks)
max_requests = 1000
max_requests_jitter = 50

# Restart workers gracefully
worker_tmp_dir = "/dev/shm"  # Use shared memory for better performance


def on_starting(server):
    """Called just before the master process is initialized."""
    pass


def on_reload(server):
    """Called to recycle workers during a reload via SIGHUP."""
    pass


def when_ready(server):
    """Called just after the server is started."""
    pass


def pre_fork(server, worker):
    """Called just before a worker is forked."""
    pass


def post_fork(server, worker):
    """Called just after a worker has been forked."""
    pass


def post_worker_init(worker):
    """Called just after a worker has initialized the application."""
    pass


def worker_int(worker):
    """Called when a worker receives SIGINT or SIGQUIT."""
    pass


def worker_abort(worker):
    """Called when a worker receives SIGABRT."""
    pass


def pre_exec(server):
    """Called just before a new master process is forked."""
    pass


def child_exit(server, worker):
    """Called when a worker process exits."""
    pass


def worker_exit(server, worker):
    """Called just after a worker has been exited."""
    pass


def nworkers_changed(server, new_value, old_value):
    """Called when the number of workers changes."""
    pass
