"""Integration tests for /api/v1/vcps endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_vcps_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/vcps")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_create_vcp(client: AsyncClient, fleet_id: str) -> None:
    payload = {
        "vcpId": "vcp-api-001",
        "chargePointId": "CP-API-001",
        "fleetId": fleet_id,
    }
    resp = await client.post("/api/v1/vcps", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["vcpId"] == "vcp-api-001"
    assert data["chargePointId"] == "CP-API-001"
    assert data["actualState"] == "unknown"
    assert data["connectionState"] == "disconnected"
    assert data["fleetId"] == fleet_id
    assert data["profileId"] == "test-profile"       # inherited from the fleet
    assert data["workerId"] == f"fleet-{fleet_id}"


@pytest.mark.asyncio
async def test_create_vcp_requires_fleet(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/vcps", json={"vcpId": "x", "chargePointId": "CP-X"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_vcp_unknown_fleet(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/vcps", json={"chargePointId": "CP-X", "fleetId": "nope"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_vcp_auto_id_and_redeploy_flag(client: AsyncClient, fleet_id: str) -> None:
    before = (await client.get(f"/api/v1/fleets/{fleet_id}")).json()
    resp = await client.post("/api/v1/vcps", json={"chargePointId": "CP-AUTO", "fleetId": fleet_id})
    assert resp.status_code == 201
    assert resp.json()["vcpId"] == "TF-00003"            # next free index after TF-00001..2
    after = (await client.get(f"/api/v1/fleets/{fleet_id}")).json()
    assert after["vcpCount"] == 3
    assert after["configVersion"] == before["configVersion"] + 1


@pytest.mark.asyncio
async def test_command_type_validated(client: AsyncClient, fleet_id: str) -> None:
    resp = await client.post("/api/v1/vcps/TF-00001/commands", json={"commandType": "teleport"})
    assert resp.status_code == 422
    resp = await client.post("/api/v1/vcps/TF-00001/commands",
                             json={"commandType": "reset", "payload": {"type": "Soft"}})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_vcp(client: AsyncClient, fleet_id: str) -> None:
    # Create first
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-get-1", "chargePointId": "CP-G1", "fleetId": fleet_id})
    resp = await client.get("/api/v1/vcps/vcp-get-1")
    assert resp.status_code == 200
    assert resp.json()["vcpId"] == "vcp-get-1"


@pytest.mark.asyncio
async def test_get_vcp_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/vcps/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_vcp(client: AsyncClient, fleet_id: str) -> None:
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-patch-1", "chargePointId": "CP-P1", "fleetId": fleet_id})
    resp = await client.patch("/api/v1/vcps/vcp-patch-1", json={"desiredState": "charging"})
    assert resp.status_code == 200
    assert resp.json()["desiredState"] == "charging"


@pytest.mark.asyncio
async def test_delete_vcp(client: AsyncClient, fleet_id: str) -> None:
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-del-1", "chargePointId": "CP-D1", "fleetId": fleet_id})
    resp = await client.delete("/api/v1/vcps/vcp-del-1")
    assert resp.status_code == 200
    assert "deregistered" in resp.json()["message"]

    # Should be gone
    resp2 = await client.get("/api/v1/vcps/vcp-del-1")
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_list_vcps_filter_by_group(client: AsyncClient, fleet_id: str) -> None:
    await client.post("/api/v1/vcps", json={
        "vcpId": "vcp-group-1", "chargePointId": "CP-GG1", "fleetId": fleet_id, "groupId": "grp-test"
    })
    resp = await client.get("/api/v1/vcps", params={"groupId": "grp-test"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(v["vcpId"] == "vcp-group-1" for v in items)


@pytest.mark.asyncio
async def test_get_vcp_commands_empty(client: AsyncClient, fleet_id: str) -> None:
    await client.post("/api/v1/vcps", json={"vcpId": "vcp-cmds-1", "chargePointId": "CP-C1", "fleetId": fleet_id})
    resp = await client.get("/api/v1/vcps/vcp-cmds-1/commands")
    assert resp.status_code == 200
    assert resp.json() == []
