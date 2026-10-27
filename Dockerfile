# syntax=docker/dockerfile:1
# Stage 1: build the single-page app (type-check, then bundle).
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: the API, which also serves the built app on the same origin.
FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Dependencies come from pyproject.toml (the only list). Add `.[aws]` extras with --build-arg EXTRAS=aws.
ARG EXTRAS=""
COPY backend/pyproject.toml /tmp/pyproject.toml
RUN python - <<'PY' > /tmp/requirements.txt
import tomllib
p = tomllib.load(open("/tmp/pyproject.toml", "rb"))["project"]
import os
extras = [e for e in os.environ.get("EXTRAS", "").split(",") if e]
print("\n".join(p["dependencies"] + [d for e in extras for d in p.get("optional-dependencies", {}).get(e, [])]))
PY
RUN pip install -r /tmp/requirements.txt

COPY backend/app ./backend/app
COPY config ./config
COPY data/results ./data/results
COPY --from=web /web/dist ./frontend/dist

# Run as an unprivileged user. /var/ripple is the volume for audit logs, kill-switch state and the security log;
# /app/data is scratch (generated ground truth and the model cache) and is not meant to persist.
RUN useradd --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin ripple \
 && mkdir -p /var/ripple /app/data/ground_truth \
 && chown -R ripple:ripple /var/ripple /app/data
USER ripple
ENV RIPPLE_DATA_DIR=/var/ripple PYTHONPATH=/app/backend
# RIPPLE_ENV is deliberately not set: the default is production, which refuses every request until an identity provider is configured.
VOLUME ["/var/ripple"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8).status == 200 else 1)"]
# Plain http inside the container; terminate TLS in front of it (the app sends HSTS in production).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
