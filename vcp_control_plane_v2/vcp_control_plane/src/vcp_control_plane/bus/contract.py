"""Control Plane ↔ Data Plane bus contract (v2) — control-plane copy.

Mirrors ``vcp_data_plane/contract.py``.  Both test suites pin the values
below (tests/test_contract.py) so a change on one side without the other
fails CI.  See the data-plane module for the full wire description; summary:

Commands  CP → worker   XADD vcp:commands:worker:{worker_id} | vcp:commands:broadcast
          fields: command_json (JSON WorkerCommand), command_type, vcp_id
Events    worker → CP   XADD vcp:events:worker:{worker_id}
          fields: event, vcp_id, worker_id, payload (JSON), ts
"""
from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "2"

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
EVT_STATE_CHANGE = "state_change"
EVT_METER_VALUE = "meter_value"
EVT_OCPP_TRACE = "ocpp_trace"
EVT_COMMAND_RESULT = "command_result"
EVT_SCENARIO_STATUS = "scenario_status"
EVT_WORKER_STATUS = "worker_status"

EVENT_TYPES = (
    EVT_STATE_CHANGE,
    EVT_METER_VALUE,
    EVT_OCPP_TRACE,
    EVT_COMMAND_RESULT,
    EVT_SCENARIO_STATUS,
    EVT_WORKER_STATUS,
)

# ---------------------------------------------------------------------------
# Commands — catalogue served at GET /api/v1/commands (drives the UI)
# ---------------------------------------------------------------------------
# category: connection | session | operator | fault | meter | scenario | introspection
COMMAND_CATALOGUE: list[dict[str, Any]] = [
    # Connection
    {"commandType": "connect", "label": "Connect", "category": "connection", "payload": {}},
    {"commandType": "disconnect", "label": "Disconnect", "category": "connection", "payload": {}},
    {"commandType": "reconnect", "label": "Reconnect", "category": "connection", "payload": {}},
    {"commandType": "send_boot_notification", "label": "Send BootNotification", "category": "connection",
     "payload": {}},
    # Charging session
    {"commandType": "plug_ev", "label": "Plug EV", "category": "session",
     "payload": {"id_tag": "", "evse_id": 1, "connector_id": 1}},
    {"commandType": "authorize", "label": "Authorize", "category": "session",
     "payload": {"id_tag": "TAG-MANUAL", "evse_id": 1, "connector_id": 1}},
    {"commandType": "start_charge", "label": "Start Charge", "category": "session",
     "payload": {"evse_id": 1, "connector_id": 1}},
    {"commandType": "stop_charge", "label": "Stop Charge", "category": "session",
     "payload": {"reason": "Local", "evse_id": 1, "connector_id": 1}},
    {"commandType": "unplug_ev", "label": "Unplug EV", "category": "session",
     "payload": {"evse_id": 1, "connector_id": 1}},
    # Operator actions (simulated locally on the charger, reported to the CSMS)
    {"commandType": "reset", "label": "Reset", "category": "operator", "payload": {"type": "Soft"},
     "options": {"type": ["Soft", "Hard"]}},
    {"commandType": "change_state", "label": "Change Availability", "category": "operator",
     "payload": {"desired_state": "Available"},
     "options": {"desired_state": ["Available", "Unavailable"]}},
    {"commandType": "unlock_connector", "label": "Unlock Connector", "category": "operator",
     "payload": {"connector_id": 1}},
    {"commandType": "get_configuration", "label": "Get Configuration", "category": "operator",
     "payload": {"keys": []}},
    # Fault injection
    {"commandType": "inject_fault", "label": "Inject Fault", "category": "fault",
     "payload": {"fault_type": "connector_fault", "error_code": "GroundFailure", "connector_id": 1},
     "options": {"fault_type": [
         "network_disconnect", "reconnect_storm", "message_timeout", "malformed_payload",
         "connector_fault", "power_outage", "slow_response", "duplicate_messages",
     ]}},
    {"commandType": "recover", "label": "Recover", "category": "fault", "payload": {}},
    # Meter
    {"commandType": "send_meter_values", "label": "Send MeterValues", "category": "meter",
     "payload": {"evse_id": 1, "connector_id": 1}},
    # Scenario control
    {"commandType": "run_scenario", "label": "Run Scenario", "category": "scenario",
     "payload": {"scenario_id": "boot_and_wait"}},
    {"commandType": "stop_scenario", "label": "Stop Scenario", "category": "scenario", "payload": {}},
    # Introspection
    {"commandType": "get_state", "label": "Get State", "category": "introspection", "payload": {}},
]

COMMAND_TYPES = frozenset(c["commandType"] for c in COMMAND_CATALOGUE)

# ---------------------------------------------------------------------------
# Scenarios — canonical catalogue + v1 aliases
# ---------------------------------------------------------------------------


def _p(name: str, type_: str, default: Any, description: str = "") -> dict[str, Any]:
    return {"name": name, "type": type_, "default": default, "description": description}


SCENARIO_CATALOGUE: list[dict[str, Any]] = [
    {"scenarioId": "boot_and_wait", "name": "Boot and Wait",
     "description": "Confirm the VCP is booted and hold — baseline connectivity check.",
     "parameters": [_p("wait_s", "int", 60, "Hold time after boot")]},
    {"scenarioId": "plug_authorize_charge", "name": "Basic Charge Cycle",
     "description": "Plug EV → Authorize → StartTransaction → MeterValues → StopTransaction → Unplug. "
                    "Validates the full OCPP happy path.",
     "parameters": [_p("charge_duration_s", "int", 120, "Charge duration"),
                    _p("id_tag", "str", "", "RFID tag (random if blank)")],
     "aliases": ["basic_charge_cycle"]},
    {"scenarioId": "remote_start_charge", "name": "Remote Start / Stop",
     "description": "Wait for a CSMS-initiated RemoteStartTransaction, charge, then stop.",
     "parameters": [_p("wait_timeout_s", "int", 30, "Wait for RemoteStart"),
                    _p("charge_duration_s", "int", 120, "Charge duration")],
     "aliases": ["remote_start_stop"]},
    {"scenarioId": "fault_and_recover", "name": "Fault and Recover",
     "description": "Inject a fault, hold it, then recover. Tests CSMS fault handling.",
     "parameters": [_p("fault_type", "str", "connector_fault"),
                    _p("error_code", "str", "GroundFailure"),
                    _p("fault_duration_s", "float", 15.0, "Hold time in fault")]},
    {"scenarioId": "reconnect_storm", "name": "Rapid Reconnect",
     "description": "Disconnect/reconnect repeatedly to stress CSMS reconnect handling.",
     "parameters": [_p("cycles", "int", 5), _p("interval_s", "float", 2.0)],
     "aliases": ["rapid_reconnect"]},
    {"scenarioId": "load_ramp", "name": "Load Ramp",
     "description": "Start a session and hold it across ramp steps (power envelope test).",
     "parameters": [_p("ramp_steps", "int", 5), _p("step_duration_s", "int", 30)]},
    {"scenarioId": "transaction_timeout", "name": "Transaction Timeout",
     "description": "Start a session and let the profile's max transaction duration stop it.",
     "parameters": [_p("timeout_s", "int", 60)]},
    {"scenarioId": "csms_initiated_stop", "name": "CSMS-Initiated Stop",
     "description": "Charge and wait for the CSMS to send RemoteStop (stops locally on timeout).",
     "parameters": [_p("wait_for_stop_s", "int", 120)]},
    {"scenarioId": "boot_storm", "name": "Boot Storm",
     "description": "Drop and re-open the connection with jitter so every targeted VCP re-boots at once. "
                    "Run fleet-wide to surge BootNotifications at the CSMS.",
     "parameters": [_p("jitter_ms", "int", 0, "Random reconnect delay per VCP"),
                    _p("down_s", "float", 1.0, "Offline time before reconnect"),
                    _p("boot_timeout_s", "float", 60.0, "Max wait for Boot accepted")]},
    {"scenarioId": "meter_value_flood", "name": "Meter Value Flood",
     "description": "High-frequency MeterValues during a transaction — soak / load test of CSMS metering.",
     "parameters": [_p("duration_s", "int", 300), _p("interval_s", "float", 1.0)]},
]

SCENARIO_IDS = tuple(s["scenarioId"] for s in SCENARIO_CATALOGUE)

SCENARIO_ALIASES: dict[str, tuple[str, dict[str, str]]] = {
    "basic_charge_cycle": ("plug_authorize_charge", {"duration_s": "charge_duration_s"}),
    "rapid_reconnect": ("reconnect_storm", {}),
    "remote_start_stop": ("remote_start_charge", {"duration_s": "charge_duration_s"}),
}


def resolve_scenario(scenario_id: str, params: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    params = dict(params or {})
    if scenario_id in SCENARIO_ALIASES:
        canonical, renames = SCENARIO_ALIASES[scenario_id]
        for old, new in renames.items():
            if old in params and new not in params:
                params[new] = params.pop(old)
        return canonical, params
    return scenario_id, params


def scenario_entry(scenario_id: str) -> dict[str, Any] | None:
    for s in SCENARIO_CATALOGUE:
        if s["scenarioId"] == scenario_id:
            return s
    return None


# Scenario status values reported per VCP by workers
SCENARIO_TERMINAL = frozenset({"completed", "failed", "cancelled"})
