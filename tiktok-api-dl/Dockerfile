# Bun image with Debian base (needed for ffmpeg + native deps)
FROM oven/bun:1.2-debian AS base

# Install ffmpeg + ca-certificates + curl (for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifests first for layer caching
COPY package.json bun.lock ./

# Install production dependencies (frozen lockfile)
RUN bun install --frozen-lockfile --production

# Copy application source
COPY src ./src

# Verify ffmpeg is available
RUN ffmpeg -version | head -1

# Default env (overridden by docker-compose)
ENV PORT=7788
ENV DEFAULT_VERSION=v1
ENV FFMPEG_PATH=ffmpeg
ENV NODE_ENV=production

EXPOSE 7788

# Health check via /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7788/health || exit 1

# ---------- Production stage ----------
FROM base AS production

CMD ["bun", "run", "src/index.ts"]

# ---------- Development stage ----------
FROM base AS development

# Install all dependencies (including dev) for watch mode
RUN bun install --frozen-lockfile

# Mount source as volume in docker-compose for hot reload
CMD ["bun", "--watch", "run", "src/index.ts"]
