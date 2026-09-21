# DS Video stream server: ffmpeg + a small Python service.
# Build context is this folder. Needs internet access once, to download packages.
FROM alpine:3.20

# ffmpeg from Alpine includes x264, x265, VP9, Opus, MP3, zimg (HDR tone mapping) and VAAPI.
# intel-media-driver enables Intel Quick Sync (optional, x86 NAS models only).
RUN apk add --no-cache python3 ffmpeg ca-certificates \
 && if [ "$(uname -m)" = "x86_64" ]; then apk add --no-cache intel-media-driver || true; fi

WORKDIR /app
COPY planner.py streamserver.py /app/

ENV PORT=8899 \
    WORK_DIR=/cache \
    DSM_URL=http://127.0.0.1:5000 \
    PYTHONUNBUFFERED=1

VOLUME /cache
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=6s --start-period=10s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8899'), timeout=5)" || exit 1

CMD ["python3", "/app/streamserver.py"]
