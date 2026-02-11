# Stage 1: Build Rust binary
FROM rust:1.83-bookworm AS builder

# Install Python dev headers (needed for PyO3 compilation)
RUN apt-get update && apt-get install -y python3-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY serverx-rs/Cargo.toml serverx-rs/Cargo.lock* ./
COPY serverx-rs/src/ src/

# Set PyO3 to use system Python
ENV PYO3_PYTHON=python3

RUN cargo build --release

# Stage 2: Runtime
FROM python:3.11-slim-bookworm

# Copy local yt_dlp from parent directory
COPY yt_dlp /usr/local/lib/python3.11/site-packages/yt_dlp

# Copy Rust binary
COPY --from=builder /app/target/release/serverx-rs /usr/local/bin/serverx-rs
# COPY ../yt_dlp /app/yt_dlp

ENV PORT=8025
EXPOSE 8025

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:8025/health || exit 1

CMD ["serverx-rs"]
