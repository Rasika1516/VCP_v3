"""SQLAlchemy ORM models for the VCP Control Plane registry.

Five tables:
  vcp_fleet       — a deployable VCP fleet: generated worker config + runtime
  vcp_instance    — one row per virtual charge point (belongs to a fleet)
  charger_profile — versioned brand/model profile artifacts
  test_execution  — scenario run records
  command_audit   — immutable log of every command dispatched
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Text,  # noqa: F401 — used for long text columns
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import os as _os

# Use JSONB on PostgreSQL for indexing performance; fall back to plain JSON
# on SQLite (used in tests).  We detect the dialect from the database URL.
_db_url = _os.environ.get("VCP_CP_DATABASE_URL", "")
if "postgresql" in _db_url or "asyncpg" in _db_url:
    from sqlalchemy.dialects.postgresql import JSONB
else:
    JSONB = JSON  # type: ignore[assignment,misc]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# VCP Fleet
# ---------------------------------------------------------------------------

class VcpFleet(Base):
    """A deployable VCP fleet = one data-plane worker.

    The control plane generates the worker's ``worker.yaml`` from this row plus
    its ``vcp_instance`` rows, and starts / stops the worker via a launcher
    (local process or Docker container).

    status lifecycle:
        draft → deploying → running → stopping → stopped
                     └──────→ failed  (start error / worker exited)
    """

    __tablename__ = "vcp_fleet"

    fleet_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    worker_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Target CSMS
    csms_url: Mapped[str] = mapped_column(String(512))
    csms_auth: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # {type, username, password}

    # VCP generation defaults
    profile_id: Mapped[str] = mapped_column(String(64), default="")
    ocpp_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    id_prefix: Mapped[str] = mapped_column(String(32), default="VCP-")
    charge_point_id_prefix: Mapped[str] = mapped_column(String(32), default="CP-")
    id_start: Mapped[int] = mapped_column(Integer, default=1)

    # Worker options: boot_jitter_ms, forward_traces_to_events, log_level, …
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Deployment
    launcher: Mapped[str] = mapped_column(String(16), default="process")  # process | docker
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    api_port: Mapped[int] = mapped_column(Integer, default=0)
    metrics_port: Mapped[int] = mapped_column(Integer, default=0)
    worker_url: Mapped[str] = mapped_column(String(256), default="")
    config_path: Mapped[str] = mapped_column(String(512), default="")
    runtime: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)   # {pid} | {container_id}
    last_error: Mapped[str] = mapped_column(Text, default="")
    # Incremented on every change affecting the generated config; compared with
    # deployed_config_version to flag "redeploy required" in the UI
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    deployed_config_version: Mapped[int] = mapped_column(Integer, default=0)

    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# VCP Instance
# ---------------------------------------------------------------------------

class VcpInstance(Base):
    """Runtime record for a single virtual charge point.

    ``desired_state`` is what the control plane wants; ``actual_state`` is
    what the worker last reported.  Both use OCPP connector-state strings
    (Available, Charging, Faulted …) plus "Offline" / "Connecting".
    """

    __tablename__ = "vcp_instance"

    vcp_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    charge_point_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    profile_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    profile_version: Mapped[str] = mapped_column(String(32), default="latest")
    firmware: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocpp_version: Mapped[str] = mapped_column(String(16), default="1.6J")
    transport: Mapped[str] = mapped_column(String(16), default="websocket")
    security_profile: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Owning fleet (soft FK → vcp_fleet.fleet_id); null = discovered from an
    # externally managed worker
    fleet_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Desired vs actual state
    desired_state: Mapped[str] = mapped_column(String(32), default="Available")
    actual_state: Mapped[str] = mapped_column(String(32), default="Offline")
    connection_state: Mapped[str] = mapped_column(String(32), default="Disconnected")

    # Connector snapshot — JSON: {"evse1_conn1": {"state": "Charging", "transaction_id": 42, ...}}
    connector_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


# ---------------------------------------------------------------------------
# Charger Profile
# ---------------------------------------------------------------------------

class ChargerProfile(Base):
    """Versioned, brand/model charger profile artifact.

    Profiles are treated as immutable once published; creating a new version
    inserts a new row rather than updating the existing one.
    """

    __tablename__ = "charger_profile"

    profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    vendor: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    firmware: Mapped[str] = mapped_column(String(64), default="1.0.0")
    ocpp_version: Mapped[str] = mapped_column(String(16), default="1.6J")

    # Rich JSON blobs captured from YAML profile files
    protocol_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    capability_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    behavior_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    evse_definition: Mapped[list[Any]] = mapped_column(JSONB, default=list)

    source: Mapped[str] = mapped_column(Text, default="")
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str] = mapped_column(String(128), default="")
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | deprecated

    created_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Test Execution
# ---------------------------------------------------------------------------

class TestExecution(Base):
    """Record of a scenario execution campaign."""

    __tablename__ = "test_execution"

    execution_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=_new_uuid
    )
    scenario_id: Mapped[str] = mapped_column(String(64), index=True)
    scenario_name: Mapped[str] = mapped_column(String(128), default="")

    # Scope: single | group | fleet
    target_scope: Mapped[str] = mapped_column(String(16), default="single")
    target_vcp_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_count: Mapped[int] = mapped_column(Integer, default=1)

    # Status: pending | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending")
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Result: passed | failed | partial | None
    result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    initiated_by: Mapped[str] = mapped_column(String(128), default="api")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Command Audit
# ---------------------------------------------------------------------------

class CommandAudit(Base):
    """Immutable audit log entry for every command dispatched to a worker."""

    __tablename__ = "command_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    command_type: Mapped[str] = mapped_column(String(64))
    vcp_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    issued_by: Mapped[str] = mapped_column(String(128), default="api")
    # dispatched | acknowledged | failed
    status: Mapped[str] = mapped_column(String(32), default="dispatched")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Worker-reported outcome (command_result event): {ok, error, result, vcps}
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
