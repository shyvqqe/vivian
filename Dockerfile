# yt-dlp EJS requires a supported JS runtime; Debian's `apt install nodejs`
# can supply Node 18, which is too old (yt-dlp EJS now requires Node >=22).
FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Fail the image build immediately if FFmpeg / FFprobe were not installed.
RUN ffmpeg -version >/dev/null && ffprobe -version >/dev/null

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "-u", "vivian.py"]
