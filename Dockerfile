FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/kern-labs/boombox-bridge" \
      org.opencontainers.image.description="Boom Box Bridge: preset buttons for SoundTouch speakers without the cloud" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    BRIDGE_PORT=8765

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bridge/ ./bridge/

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ.get('BRIDGE_PORT','8765'), timeout=4)"

CMD ["python", "bridge/app.py"]
