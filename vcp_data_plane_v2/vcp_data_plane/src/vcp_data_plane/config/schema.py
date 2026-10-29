"""Pydantic configuration schemas for the VCP Data Plane worker.

Two distinct config objects live here:

  WorkerConfig   — loaded from worker.yaml at process start; describes the
                   worker's runtime parameters, Redis connection, CSMS target,
                   shard identity and the set of VCPs this worker owns.

  ChargerProfile — a versioned, brand/model profile artifact; loaded from
                   individual YAML files in the profiles directory. Multiple
                   profiles can be active simultaneously within one worker
                   (mixed-fleet scenarios).

The separation mirrors the architecture's key principle: identity / profile /
behavior are configuration-driven and independent from the worker runtime.
"""
from __future__ import annotations

import math
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class OcppVersion(str, Enum):
    V16 = "1.6"
    V16J = "1.6J"
    V201 = "2.0.1"


class TransportType(str, Enum):
    WEBSOCKET = "websocket"
    WEBSOCKETS = "websockets"   # alias
    WSS = "wss"


class AuthType(str, Enum):
    NONE = "none"
    BASIC = "basic"
    TLS_CERT = "tls-cert"


class SecurityProfile(int, Enum):
    UNSECURED = 0   # OCPP sec profile 0 — plain WS, no auth
    HTTP_BASIC = 1  # OCPP sec profile 1 — WS + HTTP Basic Auth
    TLS_BASIC = 2   # OCPP sec profile 2 — WSS + HTTP Basic Auth
    TLS_CERT = 3    # OCPP sec profile 3 — WSS + TLS client certificate


# ---------------------------------------------------------------------------
# Worker Config
# ---------------------------------------------------------------------------

class RedisConfig(BaseModel):
    """Redis connection used for Command Streams and VCP state cache."""
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    ssl: bool = False
    # Stream key prefixes
    command_stream_prefix: str = "vcp:commands:worker"
    broadcast_stream: str = "vcp:commands:broadcast"
    event_stream_prefix: str = "vcp:events:worker"
    trace_stream_prefix: str = "vcp:trace"
    state_hash_prefix: str = "vcp:state"
    registry_key: str = "vcp:registry"
    # Stream consumer group name (shared by all workers reading the same stream)
    consumer_group: str = "data-plane"
    # Maximum trace entries per VCP stream before MAXLEN trim
    trace_maxlen: int = 10_000
    # Maximum entries kept in the worker → control-plane event stream
    event_maxlen: int = 10_000
    # Forward every OCPP frame to the control-plane event stream as an
    # ``ocpp_trace`` event (the per-VCP vcp:trace:* stream is always written).
    # Turn off for very large fleets to reduce event-stream traffic.
    forward_traces_to_events: bool = True
    # Interval for the worker_status heartbeat event (s); 0 disables it
    worker_heartbeat_s: float = 15.0
    # Command stream read block timeout ms (0 = block indefinitely)
    stream_block_ms: int = 1_000


class CsmsAuthConfig(BaseModel):
    type: AuthType = AuthType.NONE
    username: str | None = None
    password: str | None = None
    # TLS client certificate paths (AuthType.TLS_CERT)
    client_cert: str | None = None
    client_key: str | None = None
    ca_cert: str | None = None


class CsmsConfig(BaseModel):
    """Target CSMS connection parameters."""
    url: str  # e.g. wss://csms.example.com/ocpp
    auth: CsmsAuthConfig = Field(default_factory=CsmsAuthConfig)
    connect_timeout_s: float = 10.0
    ping_interval_s: float = 20.0
    ping_timeout_s: float = 10.0
    # Reconnect back-off: exponential, clamped at max_backoff_s
    initial_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    backoff_multiplier: float = 2.0


class VcpEntry(BaseModel):
    """A single VCP instance definition within the worker fleet config."""
    vcp_id: str                          # e.g. "VCP-00042"
    charge_point_id: str                 # OCPP ChargePoint identity (unique to CSMS)
    profile_id: str                      # References a ChargerProfile.profile_id
    profile_version: str = "latest"      # "latest" or specific semver
    # Per-VCP OCPP version override (falls back to profile, then fleet default)
    ocpp_version: OcppVersion | None = None
    # Logical group (used to filter group-scoped broadcast commands)
    group_id: str | None = None


class ShardingConfig(BaseModel):
    shard_index: int = 0
    shard_count: int = 1


class ControlApiConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9090
    token: str = ""
    require_auth: bool = True


class MetricsConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9100
    enabled: bool = True


class WorkerConfig(BaseModel):
    """Top-level worker configuration (worker.yaml)."""
    worker_id: str                        # Unique worker identifier, e.g. "worker-01"
    csms: CsmsConfig
    redis: RedisConfig = Field(default_factory=RedisConfig)
    # VCPs owned by this worker; alternatively generated from fleet block
    vcps: list[VcpEntry] = Field(default_factory=list)
    # Alternatively: generate VCPs from a fleet block (range-based)
    fleet: "FleetBlock | None" = None
    # Directory containing ChargerProfile YAML files
    profile_dir: str = "configs/profiles"
    sharding: ShardingConfig = Field(default_factory=ShardingConfig)
    control_api: ControlApiConfig = Field(default_factory=ControlApiConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    # Boot jitter range in ms; stagger connection attempts to avoid boot storms
    boot_jitter_ms: tuple[int, int] = (0, 5_000)
    # Default OCPP version when not specified in profile or VCP entry
    default_ocpp_version: OcppVersion = OcppVersion.V16J
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_vcps_or_fleet(self) -> "WorkerConfig":
        if not self.vcps and self.fleet is None:
            raise ValueError("Either 'vcps' list or 'fleet' block must be provided")
        return self


class FleetBlock(BaseModel):
    """Convenience block: generate VCPs by range instead of enumerating each one."""
    count: int = Field(ge=1)
    id_prefix: str = "VCP-"
    id_start: int = 0                    # First VCP index in this worker's range
    charge_point_id_prefix: str = "CP-"
    default_profile_id: str = "generic_ac_ocpp16"
    default_ocpp_version: OcppVersion | None = None
    group_id: str | None = None

    def expand(self) -> list[VcpEntry]:
        return [
            VcpEntry(
                vcp_id=f"{self.id_prefix}{self.id_start + i:05d}",
                charge_point_id=f"{self.charge_point_id_prefix}{self.id_start + i:05d}",
                profile_id=self.default_profile_id,
                ocpp_version=self.default_ocpp_version,
                group_id=self.group_id,
            )
            for i in range(self.count)
        ]


# Fix forward reference
WorkerConfig.model_rebuild()


# ---------------------------------------------------------------------------
# Charger Profile
# ---------------------------------------------------------------------------

class EvseConnectorConfig(BaseModel):
    connector_id: int = 1
    connector_type: str = "Type2"        # IEC 62196 type string
    max_power_kw: float = 7.4
    max_current_a: float = 32.0
    max_voltage_v: float = 230.0
    phase_count: int = 1                 # 1 or 3 phase


class EvseConfig(BaseModel):
    evse_id: int = 1
    connectors: list[EvseConnectorConfig] = Field(default_factory=lambda: [EvseConnectorConfig()])
    dc: bool = False                     # True → DC fast charge
    max_power_kw: float | None = None    # EVSE-level cap; falls back to connector sum

    @property
    def effective_max_power_kw(self) -> float:
        if self.max_power_kw is not None:
            return self.max_power_kw
        return sum(c.max_power_kw for c in self.connectors)


class CapabilityConfig(BaseModel):
    remote_start: bool = True
    remote_stop: bool = True
    smart_charging: bool = False
    local_auth_list: bool = False
    reserve_now: bool = False
    unlock_connector: bool = True
    change_availability: bool = True
    get_configuration: bool = True
    change_configuration: bool = True
    diagnostics: bool = False
    firmware_update: bool = False
    data_transfer: bool = False
    trigger_message: bool = True


class MeterValueConfig(BaseModel):
    """Controls what measurands are reported in MeterValues."""
    measurands: list[str] = Field(
        default_factory=lambda: [
            "Energy.Active.Import.Register",
            "Power.Active.Import",
            "Current.Import",
            "Voltage",
        ]
    )
    unit_of_measure: str = "Wh"
    location: str = "Outlet"


class BehaviorConfig(BaseModel):
    """Profile-defined charger behavior parameters."""
    heartbeat_interval_s: int = 60
    meter_value_interval_s: int = 30
    # Authorization: how long to wait before giving up (s)
    authorize_timeout_s: float = 10.0
    # Transaction: max charging duration before auto-stop (s); 0 = unlimited
    max_transaction_duration_s: int = 0
    # Idle time between transactions (s)
    idle_between_transactions_s: tuple[float, float] = (30.0, 120.0)
    # Whether to auto-start a transaction after plug-in + authorize
    transaction_auto_start: bool = False
    # Meter value config
    meter_value: MeterValueConfig = Field(default_factory=MeterValueConfig)
    # Realistic charging ramp: power rises over ramp_duration_s to full power
    charge_ramp_duration_s: float = 30.0
    fault_injection_enabled: bool = False
    # Boot notification: how many retries before giving up
    boot_retry_interval_s: int = 30
    boot_max_retries: int = 5


class ChargerProfile(BaseModel):
    """Versioned, brand/model charger profile artifact.

    Profiles are loaded from YAML files in the configured profile_dir.
    They are treated as immutable once published: a new version creates a
    new profile rather than mutating the existing one.
    """
    profile_id: str                          # e.g. "enphase_evse_7kw_ocpp16"
    version: str = "1.0"
    # Identity fields
    vendor: str = "GenericVendor"
    model: str = "GenericModel"
    firmware: str = "1.0.0"
    serial_number_prefix: str = "SN-"
    # Protocol
    ocpp_version: OcppVersion = OcppVersion.V16J
    transport: TransportType = TransportType.WEBSOCKET
    security_profile: SecurityProfile = SecurityProfile.HTTP_BASIC
    # EVSE / connector layout
    evses: list[EvseConfig] = Field(default_factory=lambda: [EvseConfig()])
    # Capabilities exposed in BootNotification / GetConfiguration
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)
    # Behavior parameters
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    # Metadata
    source: str = ""            # Reference to documented behavior source
    validated: bool = False
    approved_by: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _version_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("profile version must not be empty")
        return v

    @property
    def primary_evse(self) -> EvseConfig:
        return self.evses[0]

    @property
    def primary_connector(self) -> EvseConnectorConfig:
        return self.primary_evse.connectors[0]

    def effective_ocpp_version(self, override: OcppVersion | None = None) -> OcppVersion:
        return override if override is not None else self.ocpp_version


# ---------------------------------------------------------------------------
# Command model (exchanged over Redis Streams)
# ---------------------------------------------------------------------------

class CommandType(str, Enum):
    # Connection control
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    # Charger state triggers
    PLUG_EV = "plug_ev"
    UNPLUG_EV = "unplug_ev"
    AUTHORIZE = "authorize"
    START_CHARGE = "start_charge"
    STOP_CHARGE = "stop_charge"
    # Fault injection
    INJECT_FAULT = "inject_fault"
    RECOVER = "recover"
    # Scenario execution
    RUN_SCENARIO = "run_scenario"
    STOP_SCENARIO = "stop_scenario"
    # Meter values
    SEND_METER_VALUES = "send_meter_values"
    # Configuration
    SEND_BOOT_NOTIFICATION = "send_boot_notification"
    # Introspection
    GET_STATE = "get_state"
    # Operator actions simulated on the charger side (contract v2).  In OCPP
    # these are CSMS→CP requests; here the operator triggers the equivalent
    # local behaviour and the VCP reports the result to the CSMS as a real
    # charger would (BootNotification, StatusNotification, ...).
    RESET = "reset"                          # payload: {type: Soft|Hard}
    CHANGE_STATE = "change_state"            # payload: {desired_state: Available|Unavailable, connector_id?}
    UNLOCK_CONNECTOR = "unlock_connector"    # payload: {connector_id?}
    GET_CONFIGURATION = "get_configuration"  # payload: {keys?: [...]}


class WorkerCommand(BaseModel):
    """A command published to a Redis Stream by the control plane."""
    command_id: str                      # UUID for idempotency / tracing
    command_type: CommandType
    # Target: one of vcp_id, group_id or fleet-wide (both None → broadcast)
    vcp_id: str | None = None
    group_id: str | None = None
    # Command-specific payload
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_by: str = ""
    issued_at: str = ""                  # ISO8601 timestamp
    # Set by the CommandListener: True when read from the broadcast stream
    _via_broadcast: bool = PrivateAttr(default=False)

    @field_validator("vcp_id", "group_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return v or None

    @field_validator("issued_by", mode="before")
    @classmethod
    def _none_to_blank(cls, v: Any) -> Any:
        return v or ""


class WorkerEvent(BaseModel):
    """An event published back to Redis by a worker."""
    event_id: str
    worker_id: str
    vcp_id: str
    event_type: str                      # e.g. "state_changed", "connection_lost"
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = ""
