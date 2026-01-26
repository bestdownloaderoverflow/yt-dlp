FROM node:20-bookworm-slim

# Install system dependencies
# Debian Bookworm comes with Python 3.11 by default
RUN apt-get update && apt-get install -y \
    ffmpeg \
    python3 \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp
RUN pip3 install --no-cache-dir --break-system-packages yt-dlp

# Verify Python version (should be 3.11+)
RUN python3 --version

# Create app directory
WORKDIR /app/serverjs

# Copy package.json and package-lock.json from serverjs directory
COPY serverjs/package*.json ./

# Install Node.js dependencies
RUN npm install --omit=dev

# Copy yt-dlp.sh and yt_dlp directory from parent directory
# This allows using the same yt-dlp.sh wrapper as local development
# Note: Now Python 3.11 is available, so local yt_dlp will work
COPY yt-dlp.sh /app/yt-dlp.sh
COPY yt_dlp/ /app/yt_dlp/
RUN chmod +x /app/yt-dlp.sh

# Copy serverjs application code
COPY serverjs/ /app/serverjs/

# Create temp directory with proper permissions
RUN mkdir -p /app/serverjs/temp && chmod 777 /app/serverjs/temp

# Create yt-dlp wrapper script (for compatibility with existing code)
# IMPORTANT: Use local yt-dlp.sh as primary (it's the updated version from parent directory)
RUN echo '#!/bin/bash' > /usr/local/bin/yt-dlp.sh && \
    echo '# Use local yt-dlp.sh from parent directory (updated version)' >> /usr/local/bin/yt-dlp.sh && \
    echo 'if [ -f /app/yt-dlp.sh ]; then' >> /usr/local/bin/yt-dlp.sh && \
    echo '  exec /app/yt-dlp.sh "$@"' >> /usr/local/bin/yt-dlp.sh && \
    echo 'elif command -v yt-dlp > /dev/null 2>&1; then' >> /usr/local/bin/yt-dlp.sh && \
    echo '  # Fallback to system yt-dlp if local not available' >> /usr/local/bin/yt-dlp.sh && \
    echo '  exec yt-dlp "$@"' >> /usr/local/bin/yt-dlp.sh && \
    echo 'else' >> /usr/local/bin/yt-dlp.sh && \
    echo '  echo "Error: yt-dlp not found" >&2' >> /usr/local/bin/yt-dlp.sh && \
    echo '  exit 1' >> /usr/local/bin/yt-dlp.sh && \
    echo 'fi' >> /usr/local/bin/yt-dlp.sh && \
    chmod +x /usr/local/bin/yt-dlp.sh

# Verify both yt-dlp installations work
RUN /app/yt-dlp.sh --version || echo "Local yt-dlp.sh check (will use system yt-dlp if fails)"
RUN /usr/local/bin/yt-dlp.sh --version

# Expose the port the app runs on
EXPOSE 3021

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:3021/health || exit 1

# Start the application
CMD ["node", "index.js"]
