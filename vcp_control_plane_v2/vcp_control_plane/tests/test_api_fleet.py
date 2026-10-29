"""Integration tests for /api/v1/fleet and /api/v1/scenarios endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_fleet_summary(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/fleet/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "vcpCount" in data
    assert "connectionStateCounts" in data
    assert "actualStateCounts" in data
    assert "workerCount" in data
    assert "shards" in data


@pytest.mark.asyncio
async def test_fleet_workers_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/fleet/workers")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_scenarios(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    ids = [s["scenarioId"] for s in data]
    # contract v2 catalogue: data-plane's 8 + boot_storm + meter_value_flood
    assert len(ids) == 10
    for sid in ("plug_authorize_charge", "reconnect_storm", "fault_and_recover",
                "boot_storm", "meter_value_flood", "remote_start_charge"):
        assert sid in ids
    aliases = {a for s in data for a in s.get("aliases", [])}
    assert {"basic_charge_cycle", "rapid_reconnect", "remote_start_stop"} <= aliases


@pytest.mark.asyncio
async def test_run_unknown_scenario(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "nonexistent_scenario",
        "targetScope": "fleet",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_fleet_scenario(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "meter_value_flood",
        "targetScope": "fleet",
    })
    assert resp.status_code == 202
    data = resp.json()
    assert data["scenarioId"] == "meter_value_flood"
    assert data["status"] == "running"
    assert "executionId" in data


@pytest.mark.asyncio
async def test_list_executions(client: AsyncClient) -> None:
    # Run one first
    await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "basic_charge_cycle",
        "targetScope": "fleet",
    })
    resp = await client.get("/api/v1/scenarios/executions")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_get_execution_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/scenarios/executions/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_execution(client: AsyncClient) -> None:
    run_resp = await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "rapid_reconnect",
        "targetScope": "fleet",
    })
    exec_id = run_resp.json()["executionId"]

    cancel_resp = await client.post(f"/api/v1/scenarios/executions/{exec_id}/cancel")
    assert cancel_resp.status_code == 200
    assert exec_id in cancel_resp.json()["message"]

    # Second cancel should 404
    cancel_resp2 = await client.post(f"/api/v1/scenarios/executions/{exec_id}/cancel")
    assert cancel_resp2.status_code == 404


@pytest.mark.asyncio
async def test_list_profiles_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/profiles")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


@pytest.mark.asyncio
async def test_create_profile(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/profiles", json={
        "profileId": "profile-test-001",
        "vendor": "TestCo",
        "model": "Charger-X",
        "version": "1.0.0",
        "ocppVersion": "1.6J",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["profileId"] == "profile-test-001"
    assert data["vendor"] == "TestCo"


@pytest.mark.asyncio
async def test_get_profile_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/profiles/no-such-profile")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_deprecate_profile(client: AsyncClient) -> None:
    await client.post("/api/v1/profiles", json={
        "profileId": "profile-dep-1",
        "vendor": "DepCo",
        "model": "Old",
    })
    resp = await client.delete("/api/v1/profiles/profile-dep-1")
    assert resp.status_code == 200
    assert "deprecated" in resp.json()["message"]
