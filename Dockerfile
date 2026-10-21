# Keep a supported Node runtime available to modern yt-dlp/EJS while running Python.
FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Node 22 is copied from the matching Debian base; ffmpeg is used for merging/conversion.
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /tmp/ahmx_downloads
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"]
