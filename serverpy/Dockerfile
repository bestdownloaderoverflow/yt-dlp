FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy parent yt_dlp directory (from build context root)
COPY yt_dlp /app/yt_dlp

# Copy serverpy files
WORKDIR /app/serverpy
COPY serverpy/requirements.txt .
COPY serverpy/*.py .
COPY serverpy/gunicorn.conf.py .
COPY serverpy/.env.example .env

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories
RUN mkdir -p temp cookies

# Expose port
EXPOSE 3021

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:3021/health')" || exit 1

# Run application
CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:app"]
