"""Pin the control-plane ↔ data-plane bus contract (v2).

The control plane carries an identical copy of these values in
vcp_control_plane/bus/contract.py and pins them in its own test suite.
"""
from __future__ import annotations

import json

import pytest

from vcp_data_plane import contract
from vcp_data_plane.command.listener import parse_command_fields
from vcp_data_plane.config.schema import CommandType, SecurityProfile, CsmsAuthConfig, WorkerCommand
from vcp_data_plane.connection.transport import build_ssl_context


def test_contract_version_and_events():
    assert contract.CONTRACT_VERSION == "2"
    assert contract.EVENT_TYPES == (
        "state_change", "meter_value", "ocpp_trace", "command_result", "scenario_status", "worker_status",
    )


def test_command_vocabulary():
    assert {c.value for c in CommandType} == {
        "connect", "disconnect", "reconnect", "plug_ev", "unplug_ev", "authorize", "start_charge",
        "stop_charge", "inject_fault", "recover", "run_scenario", "stop_scenario", "send_meter_values",
        "send_boot_notification", "get_state", "reset", "change_state", "unlock_connector", "get_configuration",
    }


def test_scenario_catalogue_and_aliases():
    assert len(contract.SCENARIO_IDS) == 10
    assert {"boot_storm", "meter_value_flood"} <= set(contract.SCENARIO_IDS)
    assert contract.resolve_scenario("basic_charge_cycle", {"duration_s": 5}) == (
        "plug_authorize_charge", {"charge_duration_s": 5})
    assert contract.resolve_scenario("rapid_reconnect", {"cycles": 2}) == ("reconnect_storm", {"cycles": 2})
    assert contract.resolve_scenario("remote_start_stop", {"duration_s": 9, "id_tag": "X"}) == (
        "remote_start_charge", {"charge_duration_s": 9, "id_tag": "X"})
    assert contract.resolve_scenario("load_ramp", {"a": 1}) == ("load_ramp", {"a": 1})


@pytest.mark.parametrize("raw,conn,expected", [
    ("Available", "Available", "idle"),
    ("Charging", "Available", "charging"),
    ("Faulted", "Available", "faulted"),
    ("Available", "Offline", "offline"),
])
def test_state_normalisation(raw, conn, expected):
    assert contract.normalise_actual_state(raw, conn) == expected


def test_connection_normalisation():
    assert contract.normalise_connection_state("Offline") == "disconnected"
    assert contract.normalise_connection_state("Recovering") == "connecting"
    assert contract.normalise_connection_state("Available") == "connected"


def test_parse_v2_envelope():
    cmd = {"command_id": "c1", "command_type": "reset", "vcp_id": "V1", "payload": {"type": "Hard"}}
    data = parse_command_fields({"command_json": json.dumps(cmd), "command_type": "reset"})
    assert WorkerCommand.model_validate(data).payload == {"type": "Hard"}


def test_parse_legacy_flat_fields():
    data = parse_command_fields({
        "command_id": "c2", "command_type": "run_scenario", "vcp_id": "", "group_id": "g1",
        "payload": json.dumps({"scenario_id": "boot_storm"}), "issued_by": "", "ts": "2026-01-01T00:00:00Z",
    })
    cmd = WorkerCommand.model_validate(data)
    assert cmd.vcp_id is None and cmd.group_id == "g1"
    assert cmd.payload["scenario_id"] == "boot_storm"
    assert cmd.issued_at == "2026-01-01T00:00:00Z"


def test_parse_ignores_non_command_entries():
    assert parse_command_fields({"foo": "bar"}) is None


@pytest.mark.parametrize("profile,tls", [
    (SecurityProfile.UNSECURED, False),
    (SecurityProfile.HTTP_BASIC, False),
    (SecurityProfile.TLS_BASIC, True),
])
def test_ssl_context_only_for_tls_profiles(profile, tls):
    assert (build_ssl_context(CsmsAuthConfig(), profile) is not None) is tls
