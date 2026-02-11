# Stage 1: Build Rust binary
FROM rust:1.85-bookworm AS builder

# Install Python dev headers (needed for PyO3 compilation)
RUN apt-get update && apt-get install -y python3-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY serverrs/Cargo.toml serverrs/Cargo.lock* ./
COPY serverrs/src/ src/

# Set PyO3 to use system Python
ENV PYO3_PYTHON=python3

RUN cargo build --release

# Stage 2: Runtime
FROM python:3.11-slim-bookworm

# Install FFmpeg (needed for slideshow generation)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy local yt_dlp from parent directory
COPY yt_dlp /usr/local/lib/python3.11/site-packages/yt_dlp

# Copy Rust binary
COPY --from=builder /app/target/release/serverrs /usr/local/bin/serverrs

# Create necessary directories
RUN mkdir -p /app/temp /app/cookies

WORKDIR /app

ENV PORT=3021
EXPOSE 3021

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:3021/health || exit 1

CMD ["serverrs"]
