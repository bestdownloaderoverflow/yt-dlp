# Use Python 3.12 slim image.
# curl_cffi impersonation support is unstable on 3.13 in this stack.
FROM python:3.12-slim AS base

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
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
COPY yt-dlp-stream/extractor ./extractor

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Verify yt-dlp can be imported
RUN python -c "import yt_dlp; print(f'yt-dlp version: {yt_dlp.version.__version__}')"

# Verify Python version
RUN python --version

# Expose port
EXPOSE 9487

# Health check for extractor TCP worker protocol
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import socket,json; s=socket.create_connection(('127.0.0.1',9487),2); s.sendall(b'{\"id\":\"hc\",\"method\":\"health\"}\\n'); d=s.recv(4096); s.close(); o=json.loads(d.decode().strip()); raise SystemExit(0 if o.get('ok') else 1)" || exit 1

# Run extractor daemon server (non-HTTP).
CMD ["python", "extractor/worker_server.py"]

# Development stage
FROM base AS development

# Install dev dependencies
RUN pip install --no-cache-dir watchfiles

# Run extractor daemon server for development as well.
CMD ["python", "extractor/worker_server.py"]
