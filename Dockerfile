FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg libstdc++6 passwd \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && ffmpeg -version | head -n 1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip && python -m pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /tmp/ahmx_downloads && useradd --system --uid 10001 --create-home appuser \
    && chown -R appuser:appuser /app /tmp/ahmx_downloads
USER appuser
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"]
