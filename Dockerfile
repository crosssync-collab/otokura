FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    OTOKURA_WEB_DIR=/app/web \
    OTOKURA_DATA=/data \
    TZ=Asia/Tokyo

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/otokura /app/backend/otokura
COPY web /app/web

EXPOSE 8686
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8686/healthz',timeout=4)"

CMD ["uvicorn", "otokura.app:app", "--host", "0.0.0.0", "--port", "8686", "--proxy-headers"]
