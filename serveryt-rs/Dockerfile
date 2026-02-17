# Stage 1: Build Rust binary
FROM rust:1.85-bookworm AS builder

# Install Python dev headers (needed for PyO3 compilation)
RUN apt-get update && apt-get install -y python3-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY serveryt-rs/Cargo.toml serveryt-rs/Cargo.lock* ./
COPY serveryt-rs/src/ src/

# Set PyO3 to use system Python
ENV PYO3_PYTHON=python3

RUN cargo build --release

# Stage 2: Runtime
FROM python:3.11-slim-bookworm

# Install ffmpeg, curl for healthcheck and dependencies for Deno
RUN apt-get update && apt-get install -y ffmpeg curl unzip && rm -rf /var/lib/apt/lists/*

# Install Deno (required JavaScript runtime for yt-dlp-ejs)
RUN curl -fsSL https://deno.land/install.sh | sh && \
    mv /root/.deno/bin/deno /usr/local/bin/deno && \
    chmod +x /usr/local/bin/deno

# Install yt-dlp-ejs (required for full YouTube support)
RUN pip install yt-dlp-ejs

# Copy local yt_dlp from parent directory
COPY yt_dlp /usr/local/lib/python3.11/site-packages/yt_dlp

# Copy Rust binary
COPY --from=builder /app/target/release/serveryt-rs /usr/local/bin/serveryt-rs

# Create necessary directories
RUN mkdir -p /app/cookies

WORKDIR /app

ENV PORT=8026
EXPOSE 8026

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:8026/health || exit 1

CMD ["serveryt-rs"]
