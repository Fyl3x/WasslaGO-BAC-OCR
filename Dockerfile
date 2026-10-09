# BAC transcript OCR - production image (Flask + gunicorn + Tesseract with Arabic)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    UPLOAD_DIR=/data/uploads

# Tesseract 5 + Arabic / English / French language data; libglib is needed by OpenCV
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng tesseract-ocr-fra libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# run as an unprivileged user; /data holds uploads + results (mount a volume there)
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data/uploads \
 && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/health || exit 1

# ONE worker on purpose: job state lives in memory and Tesseract is CPU bound.
# Threads keep the UI / status polling responsive while a document is processed.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "180", \
     "--bind", "0.0.0.0:5000", "--access-logfile", "-", "app:app"]
