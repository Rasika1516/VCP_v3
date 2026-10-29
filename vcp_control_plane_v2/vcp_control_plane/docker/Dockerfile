# =============================================================================
# VCP Control Plane — Multi-stage Docker build
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Frontend builder
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build
# Output: /app/frontend/dist/

# ---------------------------------------------------------------------------
# Stage 2: Python deps builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS python-builder

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install build wheel

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --prefix=/install .

# ---------------------------------------------------------------------------
# Stage 3: Runtime image
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="VCP Control Plane" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.description="VCP Fleet Simulator — Control & Management Plane"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:$PATH" \
    PYTHONPATH="/install/lib/python3.11/site-packages"

# Create non-root user
RUN groupadd -r vcp && useradd -r -g vcp vcp

WORKDIR /app

# Copy installed Python packages
COPY --from=python-builder /install /install

# Copy Alembic config + migrations
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Copy compiled frontend into a static/ directory the app can serve
COPY --from=frontend-builder /app/frontend/dist /app/static/

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Copy any bundled YAML charger profiles
COPY profiles/ /app/profiles/ 2>/dev/null || true

RUN chown -R vcp:vcp /app
USER vcp

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["vcp-control", "run"]
