"""Pydantic request/response schemas for all API endpoints.

Field names are camelCase (OCPP convention) in JSON; Python attributes are
snake_case.  ``populate_by_name=True`` lets tests pass either spelling.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

class OkOut(_Base):
    ok: bool = True
    message: str = ""


class ErrorOut(_Base):
    detail: str


# ---------------------------------------------------------------------------
# VCP Instance
# ---------------------------------------------------------------------------

class VcpCreateIn(_Base):
    """Register a VCP into a fleet.  The fleet is mandatory: the VCP becomes
    part of that fleet's generated worker config (live after deploy/redeploy).
    ``vcpId`` defaults to the fleet's next ``{idPrefix}{n:05d}``."""
    fleet_id: str = Field(alias="fleetId")
    vcp_id: str | None = Field(default=None, alias="vcpId")
    charge_point_id: str = Field(alias="chargePointId")
    profile_id: str | None = Field(default=None, alias="profileId")
    profile_version: str = Field(default="latest", alias="profileVersion")
    ocpp_version: str = Field(default="1.6J", alias="ocppVersion")
    worker_id: str | None = Field(default=None, alias="workerId")
    group_id: str | None = Field(default=None, alias="groupId")
    desired_state: str | None = Field(default=None, alias="desiredState")
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class VcpPatchIn(_Base):
    desired_state: str | None = Field(default=None, alias="desiredState")
    profile_id: str | None = Field(default=None, alias="profileId")
    worker_id: str | None = Field(default=None, alias="workerId")
    group_id: str | None = Field(default=None, alias="groupId")
    tags: list[str] | None = None
    meta: dict[str, Any] | None = None


class ConnectorStateOut(_Base):
    state: str
    transaction_id: int | None = Field(default=None, alias="transactionId")
    power_w: float | None = Field(default=None, alias="powerW")
    energy_wh: float | None = Field(default=None, alias="energyWh")


class VcpOut(_Base):
    vcp_id: str = Field(alias="vcpId")
    charge_point_id: str = Field(alias="chargePointId")
    profile_id: str = Field(alias="profileId")
    profile_version: str = Field(alias="profileVersion")
    ocpp_version: str = Field(alias="ocppVersion")
    worker_id: str | None = Field(alias="workerId")
    group_id: str | None = Field(alias="groupId")
    fleet_id: str | None = Field(default=None, alias="fleetId")
    desired_state: str = Field(alias="desiredState")
    actual_state: str = Field(alias="actualState")
    connection_state: str = Field(alias="connectionState")
    connector_state: dict[str, Any] = Field(alias="connectorState")
    last_seen: datetime | None = Field(alias="lastSeen")
    created_at: datetime = Field(alias="createdAt")
    tags: list[str]
    meta: dict[str, Any]


class VcpListOut(_Base):
    items: list[VcpOut]
    total: int
    page: int
    page_size: int = Field(alias="pageSize")


# ---------------------------------------------------------------------------
# VCP Commands
# ---------------------------------------------------------------------------

class VcpCommandIn(_Base):
    """Generic command body; payload is command-specific."""
    command_type: str = Field(alias="commandType")
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_by: str = Field(default="api", alias="issuedBy")


class CommandOut(_Base):
    command_id: str = Field(alias="commandId")
    command_type: str = Field(alias="commandType")
    vcp_id: str | None = Field(alias="vcpId")
    status: str


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------

class ShardSummaryOut(_Base):
    shard_index: int = Field(alias="shardIndex")
    worker_id: str | None = Field(alias="workerId")
    reachable: bool
    vcp_count: int = Field(default=0, alias="vcpCount")
    connector_state_counts: dict[str, int] = Field(
        default_factory=dict, alias="connectorStateCounts"
    )


class FleetSummaryOut(_Base):
    vcp_count: int = Field(alias="vcpCount")
    connection_state_counts: dict[str, int] = Field(
        default_factory=dict, alias="connectionStateCounts"
    )
    actual_state_counts: dict[str, int] = Field(
        default_factory=dict, alias="actualStateCounts"
    )
    worker_count: int = Field(alias="workerCount")
    shards: list[ShardSummaryOut] = Field(default_factory=list)


class BulkCommandIn(_Base):
    command_type: str = Field(alias="commandType")
    payload: dict[str, Any] = Field(default_factory=dict)
    group_id: str | None = Field(default=None, alias="groupId")
    vcp_ids: list[str] | None = Field(default=None, alias="vcpIds")
    issued_by: str = Field(default="api", alias="issuedBy")


class BulkCommandOut(_Base):
    dispatched: int
    command_ids: list[str] = Field(alias="commandIds")


# ---------------------------------------------------------------------------
# Charger Profile
# ---------------------------------------------------------------------------

class ProfileCreateIn(_Base):
    profile_id: str = Field(alias="profileId")
    version: str = Field(default="1.0.0")
    vendor: str
    model: str
    firmware: str = "1.0.0"
    ocpp_version: str = Field(default="1.6J", alias="ocppVersion")
    protocol_definition: dict[str, Any] = Field(default_factory=dict, alias="protocolDefinition")
    capability_definition: dict[str, Any] = Field(
        default_factory=dict, alias="capabilityDefinition"
    )
    behavior_definition: dict[str, Any] = Field(default_factory=dict, alias="behaviorDefinition")
    evse_definition: list[Any] = Field(default_factory=list, alias="evseDefinition")
    source: str = ""
    tags: list[str] = Field(default_factory=list)


class ProfileOut(_Base):
    profile_id: str = Field(alias="profileId")
    version: str
    vendor: str
    model: str
    firmware: str
    ocpp_version: str = Field(alias="ocppVersion")
    capability_definition: dict[str, Any] = Field(alias="capabilityDefinition")
    behavior_definition: dict[str, Any] = Field(alias="behaviorDefinition")
    evse_definition: list[Any] = Field(alias="evseDefinition")
    status: str
    validated: bool
    tags: list[str]
    created_at: datetime = Field(alias="createdAt")


class ProfileListOut(_Base):
    items: list[ProfileOut]
    total: int


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

class ScenarioRunIn(_Base):
    scenario_id: str = Field(alias="scenarioId")
    scenario_name: str = Field(default="", alias="scenarioName")
    # Scope: single | group | fleet
    target_scope: str = Field(default="single", alias="targetScope")
    target_vcp_id: str | None = Field(default=None, alias="targetVcpId")
    target_group_id: str | None = Field(default=None, alias="targetGroupId")
    parameters: dict[str, Any] = Field(default_factory=dict)
    initiated_by: str = Field(default="api", alias="initiatedBy")


class ExecutionOut(_Base):
    execution_id: str = Field(alias="executionId")
    scenario_id: str = Field(alias="scenarioId")
    scenario_name: str = Field(alias="scenarioName")
    target_scope: str = Field(alias="targetScope")
    status: str
    result: str | None
    start_time: datetime | None = Field(alias="startTime")
    end_time: datetime | None = Field(alias="endTime")
    target_count: int = Field(alias="targetCount")
    created_at: datetime = Field(alias="createdAt")
    # Per-status VCP counts reported by workers, e.g. {"completed": 20, "failed": 1}
    progress: dict[str, int] = Field(default_factory=dict)
    report: dict[str, Any] | None = None


class ExecutionListOut(_Base):
    items: list[ExecutionOut]
    total: int


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class CommandAuditOut(_Base):
    command_id: str = Field(alias="commandId")
    command_type: str = Field(alias="commandType")
    vcp_id: str | None = Field(alias="vcpId")
    payload: dict[str, Any]
    issued_by: str = Field(alias="issuedBy")
    status: str
    issued_at: datetime = Field(alias="issuedAt")
    acknowledged_at: datetime | None = Field(alias="acknowledgedAt")
    result: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# VCP Fleets (deployable worker definitions)
# ---------------------------------------------------------------------------

class CsmsAuthIn(_Base):
    type: str = "none"            # none | basic
    username: str | None = None
    password: str | None = None


class FleetOptionsIn(_Base):
    boot_jitter_ms: list[int] | None = Field(default=None, alias="bootJitterMs")
    forward_traces_to_events: bool | None = Field(default=None, alias="forwardTracesToEvents")
    worker_heartbeat_s: float | None = Field(default=None, alias="workerHeartbeatS")
    log_level: str | None = Field(default=None, alias="logLevel")


class FleetCreateIn(_Base):
    name: str
    fleet_id: str | None = Field(default=None, alias="fleetId")
    description: str = ""
    worker_id: str | None = Field(default=None, alias="workerId")
    csms_url: str = Field(alias="csmsUrl")
    csms_auth: CsmsAuthIn | None = Field(default=None, alias="csmsAuth")
    profile_id: str = Field(alias="profileId")
    ocpp_version: str | None = Field(default=None, alias="ocppVersion")
    group_id: str | None = Field(default=None, alias="groupId")
    vcp_count: int = Field(default=10, alias="vcpCount")
    id_prefix: str = Field(default="VCP-", alias="idPrefix")
    charge_point_id_prefix: str = Field(default="CP-", alias="chargePointIdPrefix")
    id_start: int = Field(default=1, alias="idStart")
    launcher: str = "process"
    options: FleetOptionsIn | None = None


class FleetPatchIn(_Base):
    name: str | None = None
    description: str | None = None
    csms_url: str | None = Field(default=None, alias="csmsUrl")
    csms_auth: CsmsAuthIn | None = Field(default=None, alias="csmsAuth")
    profile_id: str | None = Field(default=None, alias="profileId")
    ocpp_version: str | None = Field(default=None, alias="ocppVersion")
    group_id: str | None = Field(default=None, alias="groupId")
    vcp_count: int | None = Field(default=None, alias="vcpCount")
    launcher: str | None = None
    options: FleetOptionsIn | None = None


class FleetOut(_Base):
    fleet_id: str = Field(alias="fleetId")
    name: str
    description: str
    worker_id: str = Field(alias="workerId")
    csms_url: str = Field(alias="csmsUrl")
    csms_auth_type: str = Field(alias="csmsAuthType")
    csms_username: str | None = Field(default=None, alias="csmsUsername")
    profile_id: str = Field(alias="profileId")
    ocpp_version: str | None = Field(alias="ocppVersion")
    group_id: str | None = Field(alias="groupId")
    id_prefix: str = Field(alias="idPrefix")
    charge_point_id_prefix: str = Field(alias="chargePointIdPrefix")
    id_start: int = Field(alias="idStart")
    options: dict[str, Any]
    launcher: str
    status: str
    api_port: int = Field(alias="apiPort")
    metrics_port: int = Field(alias="metricsPort")
    worker_url: str = Field(alias="workerUrl")
    config_path: str = Field(alias="configPath")
    runtime: dict[str, Any]
    last_error: str = Field(alias="lastError")
    config_version: int = Field(alias="configVersion")
    deployed_config_version: int = Field(alias="deployedConfigVersion")
    redeploy_required: bool = Field(alias="redeployRequired")
    vcp_count: int = Field(alias="vcpCount")
    connected_count: int = Field(alias="connectedCount")
    deployed_at: datetime | None = Field(alias="deployedAt")
    stopped_at: datetime | None = Field(alias="stoppedAt")
    last_seen: datetime | None = Field(alias="lastSeen")
    created_at: datetime = Field(alias="createdAt")


class FleetListOut(_Base):
    items: list[FleetOut]
    total: int
