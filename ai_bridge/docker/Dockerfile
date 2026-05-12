# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────
# SmartGold AI Bridge — production image
# Single-stage, slim Python 3.11. Runs uvicorn on $APP_PORT (default 8080).
# ─────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps for wheels that need a C toolchain at install time.
# yfinance pulls in pandas/numpy — those ship manylinux wheels so we rarely
# need gcc, but keep it lean with build-essential only if required.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so subsequent code edits don't bust this cache layer.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy application source.
COPY app ./app
COPY scripts ./scripts
COPY pyproject.toml ./

# Run as a non-root user.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

# Default data dir & sqlite location — override via env or compose volume.
ENV SQLITE_PATH=/app/data/signals.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

EXPOSE 8080

# Healthcheck uses stdlib to avoid an extra dep.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read(); sys.exit(0)" \
    || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
