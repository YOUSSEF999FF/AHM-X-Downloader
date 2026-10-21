FROM node:22-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" YTDLP_NODE_PATH=/usr/local/bin/node
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates curl tini tzdata && rm -rf /var/lib/apt/lists/* && python3 -m venv /opt/venv
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN /opt/venv/bin/pip install --upgrade pip setuptools wheel && /opt/venv/bin/pip install -r requirements.txt
COPY . .
RUN mkdir -p /app/data /tmp/ahmx-v4 && chmod -R 755 /app /tmp/ahmx-v4
EXPOSE 10000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --workers 1"]
