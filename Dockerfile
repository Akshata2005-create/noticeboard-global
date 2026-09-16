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
# --worker-class gthread + --threads: the public Display now holds one
# long-lived Server-Sent Events connection open per viewer (see /api/events
# in app.py). Plain sync workers would let one open Display tab occupy an
# entire worker process indefinitely, starving the admin portal. Threaded
# workers let each process serve several requests (including SSE streams)
# concurrently, with no extra dependency (gthread ships with gunicorn).
CMD ["gunicorn", "-w", "2", "--worker-class", "gthread", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
