# -- Build stage --
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir hatchling

# Copy project files
COPY pyproject.toml ./
COPY sdk/ sdk/
COPY core/ core/
COPY agent/ agent/
COPY adapters/ adapters/
COPY fixtures/ fixtures/

# Build wheel and install
RUN pip install --no-cache-dir ".[server]"

# -- Runtime stage --
FROM python:3.11-slim

# Security: run as non-root
RUN groupadd -r smartspaces && useradd -r -g smartspaces smartspaces

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY sdk/ sdk/
COPY core/ core/
COPY agent/ agent/
COPY adapters/ adapters/
COPY fixtures/ fixtures/

# Create data directory for SQLite
RUN mkdir -p /data && chown smartspaces:smartspaces /data

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

USER smartspaces

ENTRYPOINT ["python", "-m", "core.engine"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--db-path", "/data/state.db"]
