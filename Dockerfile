# Self-host on your LAN, where it can reach Plex directly.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first (better layer caching). gunicorn is the production server.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# App code.
COPY setlist_to_plex.py web.py ./
COPY templates/ templates/
COPY static/ static/

# Runs as root so it can write to a bind-mounted Unraid appdata share
# (typically owned by nobody:users) — the standard Unraid container pattern.
# History lives in a mounted volume.
RUN mkdir -p /data
ENV SETLIST_TO_PLEX_HISTORY=/data/history.json

EXPOSE 5001

# One worker keeps the in-process MusicBrainz rate-limiter effective; threads
# handle concurrency; the long timeout covers the buy-list's lazy enrichment.
CMD ["gunicorn", "web:app", "--bind", "0.0.0.0:5001", \
     "--workers", "1", "--threads", "4", "--timeout", "120"]
