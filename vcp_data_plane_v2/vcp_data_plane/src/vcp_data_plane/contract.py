"""Control Plane ↔ Data Plane bus contract (v2).

This module is the single reference for everything that crosses Redis between
``vcp_control_plane`` and ``vcp_data_plane``.  The control plane keeps an
identical copy in ``vcp_control_plane/bus/contract.py``; both test suites pin
the values below so a drift on either side fails CI.

Commands  (Control Plane → Worker)
----------------------------------
Streams:  ``vcp:commands:worker:{worker_id}``  (targeted)
          ``vcp:commands:broadcast``           (fleet / group)
Entry fields:
    command_json   — JSON-encoded ``WorkerCommand``
                     {command_id, command_type, vcp_id, group_id, payload,
                      issued_by, issued_at}
    command_type   — duplicated for readability in ``redis-cli`` (optional)
    vcp_id         — duplicated for readability (optional)
Workers also accept the legacy v1 flat layout (command_id, command_type,
vcp_id, group_id, payload(JSON), issued_by, ts) so old publishers keep working.

Each worker reads the broadcast stream through its *own* consumer group
(``{consumer_group}:{worker_id}``) so every worker receives every broadcast.

Events  (Worker → Control Plane)
--------------------------------
Stream:   ``vcp:events:worker:{worker_id}``
Entry fields (all strings):
    event      — one of EVENT_TYPES
    vcp_id     — VCP identifier ("" for worker-level events)
    worker_id  — publishing worker
    payload    — JSON-encoded event body
    ts         — ISO-8601 UTC timestamp
"""
from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "2"

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------
EVT_STATE_CHANGE = "state_change"        # VCP connection / connector state changed
EVT_METER_VALUE = "meter_value"          # MeterValues sent to CSMS
EVT_OCPP_TRACE = "ocpp_trace"            # One OCPP frame (switchable)
EVT_COMMAND_RESULT = "command_result"    # Outcome of a command (ack / fail)
EVT_SCENARIO_STATUS = "scenario_status"  # Scenario progress per VCP
EVT_WORKER_STATUS = "worker_status"      # Worker online / heartbeat / offline

EVENT_TYPES = (
    EVT_STATE_CHANGE,
    EVT_METER_VALUE,
    EVT_OCPP_TRACE,
    EVT_COMMAND_RESULT,
    EVT_SCENARIO_STATUS,
    EVT_WORKER_STATUS,
)

# ---------------------------------------------------------------------------
# Normalised state vocabulary used in state_change payloads
# ---------------------------------------------------------------------------
# connection_state  : connected | connecting | disconnected
# actual_state      : idle | preparing | authorized | charging | suspended |
#                     finishing | faulted | unavailable | offline | unknown
_ACTUAL_STATE_MAP = {
    "Available": "idle",
    "Preparing": "preparing",
    "Authorized": "authorized",
    "Charging": "charging",
    "Suspended": "suspended",
    "Finishing": "finishing",
    "Faulted": "faulted",
    "Recovering": "unknown",
    "Offline": "offline",
}


def normalise_connection_state(raw: str) -> str:
    if raw == "Offline":
        return "disconnected"
    if raw == "Recovering":
        return "connecting"
    return "connected"


def normalise_actual_state(primary_connector_state: str, connection_raw: str) -> str:
    if connection_raw == "Offline":
        return "offline"
    return _ACTUAL_STATE_MAP.get(primary_connector_state, "unknown")


# ---------------------------------------------------------------------------
# Scenario catalogue (canonical IDs + v1 aliases)
# ---------------------------------------------------------------------------
SCENARIO_IDS = (
    "boot_and_wait",
    "plug_authorize_charge",
    "remote_start_charge",
    "fault_and_recover",
    "reconnect_storm",
    "load_ramp",
    "transaction_timeout",
    "csms_initiated_stop",
    "boot_storm",
    "meter_value_flood",
)

# alias → (canonical id, {alias param name: canonical param name})
SCENARIO_ALIASES: dict[str, tuple[str, dict[str, str]]] = {
    "basic_charge_cycle": ("plug_authorize_charge", {"duration_s": "charge_duration_s"}),
    "rapid_reconnect": ("reconnect_storm", {}),
    "remote_start_stop": ("remote_start_charge", {"duration_s": "charge_duration_s"}),
}


def resolve_scenario(scenario_id: str, params: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Map an alias to its canonical scenario ID and translate parameter names."""
    params = dict(params or {})
    if scenario_id in SCENARIO_ALIASES:
        canonical, renames = SCENARIO_ALIASES[scenario_id]
        for old, new in renames.items():
            if old in params and new not in params:
                params[new] = params.pop(old)
        return canonical, params
    return scenario_id, params


# Keys in a run_scenario payload that are routing metadata, not parameters
RUN_SCENARIO_RESERVED_KEYS = frozenset({"scenario_id", "scenario", "params", "execution_id", "group_id"})
