# Stage: materialize node_modules (GitHub deps need bun/npm)
FROM oven/bun:1.2-debian AS deps
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production

# Deno runtime with Debian base (needed for ffmpeg + native npm deps)
FROM denoland/deno:2.3.1 AS base

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /deno-dir /tmp \
    && chown -R deno:deno /app /deno-dir

WORKDIR /app

COPY --chown=deno:deno package.json deno.json ./
COPY --from=deps --chown=deno:deno /app/node_modules ./node_modules
COPY --chown=deno:deno src ./src

RUN ffmpeg -version | head -1

ENV PORT=7788
ENV DEFAULT_VERSION=v1
ENV FFMPEG_PATH=/usr/bin/ffmpeg
ENV NODE_ENV=production
ENV DENO_DIR=/deno-dir
ENV TMPDIR=/tmp
ENV SLIDESHOW_MAX_CONCURRENT=1

USER deno

EXPOSE 7788

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7788/health || exit 1

# Single source of truth for flags: deno.json tasks
# ---------- Production stage ----------
FROM base AS production

CMD ["deno", "task", "start"]

# ---------- Development stage ----------
FROM base AS development

CMD ["deno", "task", "dev"]
