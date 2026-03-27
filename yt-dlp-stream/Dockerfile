# Use Python 3.13 slim image
FROM python:3.13-slim AS base

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app:/app/yt_dlp

WORKDIR /app

# Copy yt-dlp custom from parent directory (build context is parent)
COPY yt_dlp /app/yt_dlp

# Copy application files
COPY yt-dlp-stream/requirements.txt .
COPY yt-dlp-stream/main.py .
COPY yt-dlp-stream/ytdl_manager.py .
COPY yt-dlp-stream/process_manager.py .
COPY yt-dlp-stream/internal_tunnel.py .
COPY yt-dlp-stream/security.py .
COPY yt-dlp-stream/utils.py .

# Copy directories (api, core)
COPY yt-dlp-stream/api ./api
COPY yt-dlp-stream/core ./core

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Verify yt-dlp can be imported
RUN python -c "import yt_dlp; print(f'yt-dlp version: {yt_dlp.version.__version__}')"

# Verify Python version
RUN python --version

# Expose port
EXPOSE 9487

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; r=urllib.request.urlopen('http://localhost:9487/health', timeout=2); r.read(1)" || exit 1

# Run with Granian defaults for worker/thread concurrency while preserving
# the app interface and bind address used by this deployment.
CMD ["sh", "-c", "exec granian --workers ${GRANIAN_WORKERS:-2} --interface asgi --loop uvloop --host 0.0.0.0 --port 9487 main:app"]

# Development stage
FROM base AS development

# Install dev dependencies
RUN pip install --no-cache-dir watchfiles

# Run with reload for development
CMD ["sh", "-c", "exec granian --workers ${GRANIAN_WORKERS:-2} --interface asgi --loop uvloop --host 0.0.0.0 --port 9487 --reload main:app"]
