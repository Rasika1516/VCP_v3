"""Centralised application settings loaded from environment / .env file.

All variables share the ``VCP_CP_`` prefix so they don't clash with the
data-plane worker variables (``VCP_DP_``) when both run on the same host.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VCP_CP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # API server
    # -------------------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8090
    api_token: str = ""
    require_auth: bool = True

    # -------------------------------------------------------------------------
    # PostgreSQL (async via asyncpg)
    # -------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://vcp:vcp@localhost:5432/vcp_control"

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str | None = None
    redis_ssl: bool = False
    redis_db: int = 0

    # -------------------------------------------------------------------------
    # Command/Event bus — key prefixes must match vcp_data_plane settings
    # -------------------------------------------------------------------------
    command_stream_prefix: str = "vcp:commands:worker"
    broadcast_stream: str = "vcp:commands:broadcast"
    event_stream_prefix: str = "vcp:events:worker"
    consumer_group: str = "control-plane"
    consumer_name: str = "cp-01"
    # How long to block on XREADGROUP (ms); 0 = forever
    stream_block_ms: int = 1_000
    # How many events to pull per read
    stream_batch_size: int = 50

    # -------------------------------------------------------------------------
    # Data-plane workers that are NOT deployed by this control plane
    # (fleets deployed from the UI are tracked in the vcp_fleet table)
    # -------------------------------------------------------------------------
    # Preferred: explicit id=url pairs — "worker-01=http://w1:8080,worker-02=http://w2:8080"
    workers_raw: str = Field(default="", alias="VCP_CP_WORKERS")
    # Legacy: URLs only — worker IDs are resolved from each worker's GET /health
    worker_urls_raw: str = Field(default="", alias="VCP_CP_WORKER_URLS")
    worker_token: str = ""

    # Event stream discovery: also SCAN Redis for vcp:events:worker:* so workers
    # started outside the control plane are picked up without configuration.
    event_stream_discovery: bool = True
    # How often the EventSubscriber refreshes its set of worker event streams (s)
    event_stream_refresh_s: float = 5.0

    @field_validator("worker_urls_raw", "workers_raw", mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> str:
        return str(v) if v is not None else ""

    @property
    def worker_urls(self) -> list[str]:
        """All statically configured worker URLs (both settings forms)."""
        urls = [u.strip() for u in self.worker_urls_raw.split(",") if u.strip()]
        urls += [u for u in self.static_workers.values() if u not in urls]
        return urls

    @property
    def static_workers(self) -> dict[str, str]:
        """worker_id → URL from VCP_CP_WORKERS."""
        out: dict[str, str] = {}
        for item in self.workers_raw.split(","):
            item = item.strip()
            if "=" in item:
                wid, url = item.split("=", 1)
                if wid.strip() and url.strip():
                    out[wid.strip()] = url.strip()
        return out

    # -------------------------------------------------------------------------
    # VCP Fleet deployment (UI → worker.yaml → worker process / container)
    # -------------------------------------------------------------------------
    # Where generated worker configs + logs live (one sub-directory per fleet)
    fleet_config_dir: str = "var/fleets"
    # Fallback directory of data-plane profile YAMLs (used when a profile is not
    # in the control-plane registry); e.g. ../vcp_data_plane/configs/profiles
    worker_profiles_dir: str = ""
    # Default launcher for new fleets: "process" | "docker"
    default_launcher: str = "process"
    # Process launcher: command used to start a worker ("vcp-worker run <cfg>")
    worker_command: str = "vcp-worker"
    # Host the control plane uses to reach process-launched workers
    worker_host: str = "127.0.0.1"
    # Port ranges allocated to fleets (worker API / Prometheus)
    worker_api_port_start: int = 8100
    worker_metrics_port_start: int = 9300
    # Redis endpoint *as seen from the worker* (defaults to redis_host/port)
    worker_redis_host: str = ""
    worker_redis_port: int = 0
    # Docker launcher
    docker_socket: str = "/var/run/docker.sock"
    docker_image: str = "vcp-data-plane:latest"
    docker_network: str = ""
    # Host path of fleet_config_dir for bind mounts (when the control plane
    # itself runs in a container, this differs from fleet_config_dir)
    docker_host_fleet_dir: str = ""
    # Seconds to wait for a deployed worker to announce itself
    deploy_ready_timeout_s: float = 30.0

    # -------------------------------------------------------------------------
    # Frontend / CORS
    # -------------------------------------------------------------------------
    frontend_url: str = "http://localhost:5173"

    # -------------------------------------------------------------------------
    # Profile directory (for YAML profile import)
    # -------------------------------------------------------------------------
    profile_dir: str = "configs/profiles"

    # -------------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------------
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
