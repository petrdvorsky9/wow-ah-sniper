FROM python:3.12-slim

# curl is required at runtime — item_report.py shells out to it for Wowhead lookups
# (Python's requests library gets blocked by Wowhead's bot detection; curl doesn't).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8080

# Single worker with threads (not multiple worker processes) so the in-memory caches
# in item_report.py/webapp.py (Wowhead metadata, flask overview, etc.) are actually
# shared across concurrent requests instead of duplicated per-process.
#
# Reads $PORT with an 8080 fallback so this same image works unmodified on both
# Fly.io (fixed internal_port, no PORT env var set) and Render (injects PORT,
# defaulting to 10000) — shell form (not exec/JSON form) so the substitution runs.
CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 60 webapp:app
