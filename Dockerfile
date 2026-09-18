FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV TESSERACT_CMD=/usr/bin/tesseract
EXPOSE 5000
# A single worker process + several threads instead of multiple worker
# processes: on a small/free-tier host (limited RAM), each extra gunicorn
# worker is a full separate copy of the Python process (Flask, PyMuPDF,
# Tesseract, Pillow, SQLAlchemy, etc. all loaded again) — that adds up fast
# on ~512MB. Threads within one process share that memory instead, and are
# enough for this app's mostly I/O-bound workload (DB reads/writes, file
# uploads, the SSE stream) to serve the admin portal and multiple open
# Display tabs concurrently without doubling RAM use.
# --timeout 3700: Gunicorn's default 30s worker timeout is meant for
# normal short HTTP requests. It doesn't know /api/events is SUPPOSED to
# stay open for a long time, so without raising this it kills and
# reconnects the SSE stream roughly every 30 seconds, degrading live sync
# into slow polling. 3700s comfortably covers the SSE stream's own hourly
# self-recycle (SSE_MAX_CONNECTION_SECONDS in app.py) while still letting
# Gunicorn auto-restart the worker if some other request ever gets truly
# stuck (an actual bug), instead of disabling that safety net altogether.
CMD ["gunicorn", "-w", "1", "--worker-class", "gthread", "--threads", "4", "--timeout", "3700", "-b", "0.0.0.0:5000", "app:app"]
