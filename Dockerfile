FROM python:3.12-slim

# tzdata is needed for the TZ env var (below) to actually affect
# datetime.now() — the slim base doesn't ship it, and without it
# CAPTURE_HOUR/MINUTE would silently be interpreted as UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# pdfplumber pulls in pdfminer.six, which is pure Python — no poppler/system
# binary needed, which keeps this simple to build on arm (Pi).
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

ENV DATA_DIR=/data
VOLUME ["/data"]

CMD ["python", "main.py"]
