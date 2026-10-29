# =============================================================================
# VCP Data Plane — Multi-stage Dockerfile
# =============================================================================
# Stage 1 (builder) : install Python deps into a venv
# Stage 2 (runtime) : copy only the venv + source, run as non-root
# =============================================================================

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System build dependencies (needed for some native wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the dependency manifest first (layer-cache friendly)
COPY pyproject.toml .
COPY src/ src/

# Create a virtual environment and install the package + all dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install --no-cache-dir .

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Security: run as a dedicated non-root user
RUN groupadd --gid 1001 vcpworker && \
    useradd  --uid 1001 --gid 1001 --no-create-home vcpworker

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Application source (already installed into venv, but keep for introspection)
WORKDIR /app
COPY --from=builder /build/src /app/src

# Default config and profiles (can be overridden with a volume mount)
COPY configs/ /app/configs/

# Use non-root user
USER vcpworker

# ── Ports ────────────────────────────────────────────────────────────────────
# Control API
EXPOSE 8080
# Prometheus metrics
EXPOSE 9090

# ── Health check ─────────────────────────────────────────────────────────────
# Liveness probe via the /health endpoint (curl is available in slim image)
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# ── Default command ───────────────────────────────────────────────────────────
# Overridden in docker-compose / Kubernetes manifests via CMD or args.
# The config path must be mounted into the container (see docker-compose.yml).
ENTRYPOINT ["vcp-worker"]
CMD ["run", "/app/configs/worker.yaml"]
