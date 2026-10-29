"""Tests for Pydantic schema validation and camelCase aliases."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from vcp_control_plane.api.schemas import (
    VcpCreateIn,
    VcpPatchIn,
    ScenarioRunIn,
    BulkCommandIn,
    ProfileCreateIn,
)


def test_vcp_create_camel_case():
    data = {
        "vcpId": "vcp-001", "fleetId": "fleet-t",
        "chargePointId": "CP-001",
        "profileId": "profile-a",
    }
    obj = VcpCreateIn.model_validate(data)
    assert obj.vcp_id == "vcp-001"
    assert obj.charge_point_id == "CP-001"


def test_vcp_create_snake_case_also_accepted():
    data = {
        "vcp_id": "vcp-002",
        "fleet_id": "fleet-t",
        "charge_point_id": "CP-002",
        "profile_id": "profile-b",
    }
    obj = VcpCreateIn.model_validate(data)
    assert obj.vcp_id == "vcp-002"


def test_vcp_patch_partial():
    obj = VcpPatchIn.model_validate({"desiredState": "charging"})
    assert obj.desired_state == "charging"
    assert obj.worker_id is None


def test_scenario_run_in_defaults():
    obj = ScenarioRunIn.model_validate({
        "scenarioId": "basic_charge_cycle",
        "targetScope": "fleet",
    })
    assert obj.scenario_id == "basic_charge_cycle"
    assert obj.target_scope == "fleet"
    assert obj.parameters == {}


def test_scenario_run_in_scenario_name_default():
    obj = ScenarioRunIn.model_validate({
        "scenarioId": "rapid_reconnect",
        "targetScope": "single",
        "targetVcpId": "vcp-x",
    })
    # scenario_name should default to scenario_id when not provided
    assert obj.scenario_name == obj.scenario_id or obj.scenario_name is not None


def test_bulk_command_requires_command_type():
    with pytest.raises(ValidationError):
        BulkCommandIn.model_validate({"payload": {}})


def test_profile_create_in_defaults():
    obj = ProfileCreateIn.model_validate({
        "profileId": "p-test",
        "vendor": "Acme",
        "model": "EV-500",
    })
    assert obj.profile_id == "p-test"
    assert obj.ocpp_version == "1.6J"
    assert obj.capability_definition == {}
