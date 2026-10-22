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

# Build deps for any package that needs to compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
# We copy the SDK in early to leverage Docker layer caching
COPY sdk/ ./sdk/

# Install into a venv we can copy to the runtime stage.
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt && \
    /opt/venv/bin/pip install -e ./sdk

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Create a non-root user. Running as root in a container is a
# security smell; this costs us nothing and hardens the image.
RUN groupadd --system --gid 1001 atc && \
    useradd  --system --uid 1001 --gid atc --create-home atc

WORKDIR /app

# Copy the venv from the builder.
COPY --from=builder /opt/venv /opt/venv

# Copy the application code.
COPY app/ ./app/
COPY run.py ./

# Drop privileges.
USER atc

EXPOSE 8000

# Healthcheck so compose / orchestrators can tell if we're alive.
# CRITICAL: Use $PORT env var for Render
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        r = urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", 8000)}/health', timeout=3); \
        sys.exit(0 if r.status == 200 else 1)"

# CRITICAL: Use $PORT so Render can route traffic correctly
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}