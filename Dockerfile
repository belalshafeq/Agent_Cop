# ============================================================
# AgentTrafficCop — Dockerfile
#
# Multi-stage build:
#   * builder  — installs Python deps into a virtualenv
#   * runtime  — copies the venv and app code, runs as non-root
#
# Build:  docker build -t agenttrafficcop:local .
# Run:    docker run --rm -p 8000:8000 --env-file .env agenttrafficcop:local
# ============================================================

# ---------- Stage 1: builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
COPY sdk/ ./sdk/

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt && \
    /opt/venv/bin/pip install -e ./sdk

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 1001 atc && \
    useradd  --system --uid 1001 --gid atc --create-home atc

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY app/ ./app/
COPY run.py ./

USER atc

EXPOSE 8000

# FIX (#1): the original HEALTHCHECK ran a Python snippet that called
# os.environ.get(...) without ever importing `os` → NameError on every
# probe → container permanently unhealthy → orchestrator stops routing
# traffic. Single-quoted python -c with no backslashes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c 'import os,sys,urllib.request; url="http://127.0.0.1:"+str(os.environ.get("PORT","8000"))+"/health"; sys.exit(0 if urllib.request.urlopen(url,timeout=3).status==200 else 1)'

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}