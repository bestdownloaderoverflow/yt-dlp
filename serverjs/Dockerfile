FROM node:20-bullseye-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    python3 \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp
RUN pip3 install --no-cache-dir yt-dlp

# Create app directory
WORKDIR /app/serverjs

# Copy package.json and package-lock.json from serverjs directory
COPY serverjs/package*.json ./

# Install Node.js dependencies
RUN npm install --omit=dev

# Copy yt-dlp.sh and yt_dlp directory from parent directory
# This allows using the same yt-dlp.sh wrapper as local development
COPY yt-dlp.sh /app/yt-dlp.sh
COPY yt_dlp/ /app/yt_dlp/
RUN chmod +x /app/yt-dlp.sh

# Copy serverjs application code
COPY serverjs/ /app/serverjs/

# Create temp directory with proper permissions
RUN mkdir -p /app/serverjs/temp && chmod 777 /app/serverjs/temp

# Create yt-dlp wrapper script (for compatibility with existing code)
# Use the yt-dlp.sh from /app (copied from parent directory) or fallback to system yt-dlp
RUN echo '#!/bin/bash' > /usr/local/bin/yt-dlp.sh && \
    echo 'if [ -f /app/yt-dlp.sh ]; then' >> /usr/local/bin/yt-dlp.sh && \
    echo '  exec /app/yt-dlp.sh "$@"' >> /usr/local/bin/yt-dlp.sh && \
    echo 'else' >> /usr/local/bin/yt-dlp.sh && \
    echo '  exec yt-dlp "$@"' >> /usr/local/bin/yt-dlp.sh && \
    echo 'fi' >> /usr/local/bin/yt-dlp.sh && \
    chmod +x /usr/local/bin/yt-dlp.sh

# Verify both yt-dlp installations work
RUN /app/yt-dlp.sh --version || echo "Local yt-dlp.sh not working, will use system yt-dlp"
RUN yt-dlp --version

# Expose the port the app runs on
EXPOSE 3021

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:3021/health || exit 1

# Start the application
CMD ["node", "index.js"]
