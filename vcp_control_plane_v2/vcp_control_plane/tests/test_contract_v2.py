"""Contract v2 tests for the control plane: bus contract, EventSubscriber, fleets.

Uses a temp-file SQLite DB (committed data visible across sessions) so the
EventSubscriber's own sessions and the API see the same rows.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vcp_control_plane.bus import contract
from vcp_control_plane.db.models import Base
from vcp_control_plane.workers.directory import WorkerDirectory

PROFILE = {
    "profileId": "test-profile",
    "vendor": "TestVendor",
    "model": "T1",
    "ocppVersion": "1.6J",
    "evseDefinition": [{"evse_id": 1, "connectors": [{"connector_id": 1}]}],
    "behaviorDefinition": {"heartbeat_interval_s": 60},
}


@pytest_asyncio.fixture()
async def env(tmp_path, monkeypatch):
    """App + client + session factory + fakeredis + subscriber, on a fresh DB."""
    import fakeredis.aioredis

    from vcp_control_plane.api.app import create_app
    from vcp_control_plane.api.deps import get_redis
    from vcp_control_plane.bus.subscriber import EventSubscriber
    from vcp_control_plane.config.settings import get_settings
    from vcp_control_plane.db.base import get_db_session

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cp.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    settings = get_settings()
    monkeypatch.setattr(settings, "fleet_config_dir", str(tmp_path / "fleets"))

    # isolate the module-level worker directory per test
    directory = WorkerDirectory()
    import vcp_control_plane.workers.directory as dirmod
    monkeypatch.setattr(dirmod, "_directory", directory)

    app = create_app()

    async def override_db():
        async with sf() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_redis] = lambda: redis

    frames: list[dict] = []

    async def capture(frame: dict) -> None:
        frames.append(frame)

    sub = EventSubscriber(redis, settings, session_factory=sf, directory=directory, broadcast=capture)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"Authorization": "Bearer test-token"}) as client:
        yield {"client": client, "sf": sf, "redis": redis, "sub": sub, "frames": frames,
               "settings": settings, "directory": directory, "tmp": tmp_path}
    await redis.aclose()
    await engine.dispose()


async def _event(env, event: str, vcp_id: str, worker_id: str, payload: dict[str, Any]) -> None:
    """Feed one worker event through the subscriber exactly as read from Redis."""
    await env["sub"].handle_message(
        f"vcp:events:worker:{worker_id}", "0-1",
        {"event": event, "vcp_id": vcp_id, "worker_id": worker_id,
         "payload": json.dumps(payload), "ts": "2026-10-28T00:00:00Z"},
    )


async def _fleet(client, **over) -> dict:
    r = await client.post("/api/v1/profiles", json=PROFILE)
    assert r.status_code == 201, r.text
    body = {"name": "Lab A", "csmsUrl": "ws://csms:9000/ocpp", "profileId": "test-profile",
            "vcpCount": 3, "idPrefix": "LA-", "chargePointIdPrefix": "LACP-", "groupId": "g-a", **over}
    r = await client.post("/api/v1/fleets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Contract pins
# ---------------------------------------------------------------------------

def test_contract_pins():
    assert contract.CONTRACT_VERSION == "2"
    assert contract.EVENT_TYPES == (
        "state_change", "meter_value", "ocpp_trace", "command_result", "scenario_status", "worker_status",
    )
    assert len(contract.SCENARIO_IDS) == 10
    assert set(contract.SCENARIO_ALIASES) == {"basic_charge_cycle", "rapid_reconnect", "remote_start_stop"}
    assert {"reset", "change_state", "unlock_connector", "get_configuration"} <= contract.COMMAND_TYPES


def test_contract_matches_data_plane_when_installed():
    dp = pytest.importorskip("vcp_data_plane.contract")
    from vcp_data_plane.config.schema import CommandType

    assert dp.CONTRACT_VERSION == contract.CONTRACT_VERSION
    assert dp.EVENT_TYPES == contract.EVENT_TYPES
    assert set(dp.SCENARIO_IDS) == set(contract.SCENARIO_IDS)
    assert dp.SCENARIO_ALIASES == contract.SCENARIO_ALIASES
    assert {c.value for c in CommandType} == set(contract.COMMAND_TYPES)


# ---------------------------------------------------------------------------
# EventSubscriber
# ---------------------------------------------------------------------------

async def test_state_change_persists_and_auto_registers(env):
    client = env["client"]
    await _event(env, "state_change", "EXT-1", "w-ext", {
        "actual_state": "charging", "connection_state": "connected",
        "connector_state": {"evse1_conn1": {"state": "Charging"}},
        "charge_point_id": "EXT-CP-1", "profile_id": "p", "group_id": "g1",
    })
    v = (await client.get("/api/v1/vcps/EXT-1")).json()
    assert v["actualState"] == "charging" and v["connectionState"] == "connected"
    assert v["workerId"] == "w-ext" and v["groupId"] == "g1" and v["chargePointId"] == "EXT-CP-1"
    assert "auto-registered" in v["tags"]
    assert env["frames"][-1]["event"] == "state_change" and env["frames"][-1]["vcpId"] == "EXT-1"


async def test_worker_status_registers_vcps_and_marks_fleet_running(env):
    client = env["client"]
    f = await _fleet(client)
    async with env["sf"]() as s:
        from vcp_control_plane.registry.store import VcpRegistryStore
        await VcpRegistryStore(s).update_fleet(f["fleetId"], status="deploying")

    await _event(env, "worker_status", "", f["workerId"], {
        "status": "online", "contract_version": "2", "vcp_count": 3,
        "vcps": [{"vcp_id": "LA-00001", "charge_point_id": "LACP-00001"}],
    })
    fleet = (await client.get(f"/api/v1/fleets/{f['fleetId']}")).json()
    assert fleet["status"] == "running"
    w = env["directory"].get(f["workerId"])
    assert w.status == "online" and w.vcp_count == 3


async def test_command_result_acknowledges_audit(env):
    client = env["client"]
    f = await _fleet(client)
    r = await client.post("/api/v1/vcps/LA-00001/commands", json={"commandType": "get_configuration"})
    cid = r.json()["commandId"]
    # the published entry carries the v2 envelope on the fleet's worker stream
    msgs = await env["redis"].xrange(f"vcp:commands:worker:{f['workerId']}")
    assert json.loads(msgs[-1][1]["command_json"])["command_id"] == cid

    await _event(env, "command_result", "LA-00001", f["workerId"], {
        "command_id": cid, "command_type": "get_configuration", "ok": True, "error": None,
        "result": {"configuration": {"known": [{"key": "HeartbeatInterval", "value": "60"}]}},
    })
    audit = (await client.get("/api/v1/vcps/LA-00001/commands")).json()[0]
    assert audit["status"] == "acknowledged" and audit["acknowledgedAt"]
    assert audit["result"]["result"]["configuration"]["known"][0]["key"] == "HeartbeatInterval"


async def test_broadcast_command_collects_per_vcp_results(env):
    client = env["client"]
    await _fleet(client)
    r = await client.post("/api/v1/fleet/commands", json={"commandType": "get_state", "groupId": "g-a"})
    cid = r.json()["commandIds"][0]
    msgs = await env["redis"].xrange("vcp:commands:broadcast")
    assert json.loads(msgs[-1][1]["command_json"])["group_id"] == "g-a"
    for vid, ok in (("LA-00001", True), ("LA-00002", False)):
        await _event(env, "command_result", vid, "fleet-lab-a",
                     {"command_id": cid, "ok": ok, "error": None if ok else "boom", "result": {}})
    async with env["sf"]() as s:
        from vcp_control_plane.registry.store import VcpRegistryStore
        row = (await VcpRegistryStore(s).list_commands(limit=1))[0]
    assert row.status == "acknowledged"
    assert row.result["ok_count"] == 1 and row.result["total"] == 2


async def test_scenario_execution_lifecycle(env):
    client = env["client"]
    f = await _fleet(client)
    env["directory"].on_worker_status(f["workerId"], {"status": "online"})   # worker is live
    r = await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "basic_charge_cycle", "targetScope": "group", "targetGroupId": "g-a",
        "parameters": {"duration_s": 5},
    })
    assert r.status_code == 202, r.text
    ex = r.json()
    assert ex["scenarioId"] == "plug_authorize_charge" and ex["targetCount"] == 3

    # payload on the worker stream: canonical id, execution id, translated params
    cmd = json.loads((await env["redis"].xrange(f"vcp:commands:worker:{f['workerId']}"))[-1][1]["command_json"])
    assert cmd["payload"] == {"scenario_id": "plug_authorize_charge",
                              "execution_id": ex["executionId"], "charge_duration_s": 5}

    for vid, st in (("LA-00001", "completed"), ("LA-00002", "completed"), ("LA-00003", "running")):
        await _event(env, "scenario_status", vid, f["workerId"],
                     {"execution_id": ex["executionId"], "status": st, "message": ""})
    mid = (await client.get(f"/api/v1/scenarios/executions/{ex['executionId']}")).json()
    assert mid["status"] == "running" and mid["progress"] == {"completed": 2, "running": 1}

    await _event(env, "scenario_status", "LA-00003", f["workerId"],
                 {"execution_id": ex["executionId"], "status": "failed", "message": "Scenario failed: x"})
    done = (await client.get(f"/api/v1/scenarios/executions/{ex['executionId']}")).json()
    assert done["status"] == "completed" and done["result"] == "partial" and done["endTime"]
    assert done["report"]["vcps"]["LA-00003"]["status"] == "failed"


async def test_unknown_scenario_rejected(env):
    r = await env["client"].post("/api/v1/scenarios/run", json={"scenarioId": "nope", "targetScope": "fleet"})
    assert r.status_code == 422


async def test_directory_discovers_streams_and_static_workers(env, monkeypatch):
    settings = env["settings"]
    monkeypatch.setattr(settings, "workers_raw", "w-static=http://w:8080")
    await env["redis"].xadd("vcp:events:worker:w-found", {"event": "x"})
    streams = await env["sub"].refresh_streams()
    assert "vcp:events:worker:w-static" in streams
    assert "vcp:events:worker:w-found" in streams
    assert streams[-1] == "vcp:events:worker:broadcast"
    assert env["directory"].get("w-static").url == "http://w:8080"


# ---------------------------------------------------------------------------
# Fleets API
# ---------------------------------------------------------------------------

async def test_fleet_create_generates_vcps_and_config(env):
    client = env["client"]
    f = await _fleet(client, csmsAuth={"type": "basic", "username": "u", "password": "secret"})
    assert f["fleetId"] == "lab-a" and f["workerId"] == "fleet-lab-a"
    assert f["status"] == "draft" and f["vcpCount"] == 3 and f["apiPort"] >= 8100
    vcps = (await client.get("/api/v1/fleets/lab-a/vcps")).json()["items"]
    assert sorted(v["vcpId"] for v in vcps) == ["LA-00001", "LA-00002", "LA-00003"]
    assert all(v["groupId"] == "g-a" and v["fleetId"] == "lab-a" for v in vcps)

    cfg = (await client.get("/api/v1/fleets/lab-a/config")).json()
    assert cfg["worker_id"] == "fleet-lab-a"
    assert cfg["csms"]["auth"] == {"type": "basic", "username": "u", "password": "********"}
    assert [v["vcp_id"] for v in cfg["vcps"]] == ["LA-00001", "LA-00002", "LA-00003"]
    assert cfg["vcps"][0]["group_id"] == "g-a"
    assert cfg["redis"]["event_stream_prefix"] == "vcp:events:worker"
    assert cfg["control_api"]["port"] == f["apiPort"]

    # the generated config is a valid data-plane WorkerConfig
    schema = pytest.importorskip("vcp_data_plane.config.schema")
    schema.WorkerConfig.model_validate(cfg)


async def test_fleet_validation(env):
    client = env["client"]
    await _fleet(client)
    r = await client.post("/api/v1/fleets", json={"name": "Lab A", "csmsUrl": "ws://x", "profileId": "test-profile"})
    assert r.status_code == 409                                       # same slug
    r = await client.post("/api/v1/fleets", json={"name": "Other", "csmsUrl": "ws://x", "profileId": "nope"})
    assert r.status_code == 422                                       # unknown profile
    r = await client.post("/api/v1/fleets", json={"name": "Clash", "csmsUrl": "ws://x",
                                                  "profileId": "test-profile", "idPrefix": "LA-"})
    assert r.status_code == 409                                       # VCP IDs already used


async def test_fleet_resize_and_redeploy_flag(env):
    client = env["client"]
    await _fleet(client)
    r = await client.patch("/api/v1/fleets/lab-a", json={"vcpCount": 5, "csmsUrl": "ws://new:9000"})
    f = r.json()
    assert f["vcpCount"] == 5 and f["csmsUrl"] == "ws://new:9000" and f["configVersion"] == 2
    r = await client.patch("/api/v1/fleets/lab-a", json={"vcpCount": 2})
    assert r.json()["vcpCount"] == 2
    assert (await client.get("/api/v1/vcps/LA-00003")).status_code == 404


class FakeLauncher:
    """In-memory launcher used to test the deploy/stop state machine."""
    name = "process"
    alive: dict[str, bool] = {}

    def __init__(self, settings):
        self.settings = settings

    def uses_container_paths(self):
        return False

    async def check(self):
        return True, "fake"

    async def start(self, fleet, cfg_path):
        from vcp_control_plane.workers.launcher import LaunchResult
        assert Path(cfg_path).exists()
        FakeLauncher.alive[fleet.fleet_id] = True
        return LaunchResult(runtime={"pid": 4242}, worker_url=f"http://127.0.0.1:{fleet.api_port}")

    async def stop(self, fleet, timeout_s=10):
        FakeLauncher.alive[fleet.fleet_id] = False

    async def is_alive(self, fleet):
        return FakeLauncher.alive.get(fleet.fleet_id, False)

    async def logs(self, fleet, tail=200):
        return "fake log line"


async def test_fleet_deploy_stop_and_crash_detection(env, monkeypatch):
    import vcp_control_plane.api.routers.fleets as fr
    import vcp_control_plane.workers.fleet_service as fs

    monkeypatch.setattr(fs, "get_launcher", lambda name, settings: FakeLauncher(settings))
    monkeypatch.setattr(fr, "get_launcher", lambda name, settings: FakeLauncher(settings))
    client = env["client"]
    await client.post("/api/v1/profiles", json=PROFILE)
    f = await _fleet(client, name="Lab B", idPrefix="LB-", chargePointIdPrefix="LBCP-")

    r = await client.post(f"/api/v1/fleets/{f['fleetId']}/deploy")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "deploying" and d["runtime"] == {"pid": 4242}
    assert d["deployedConfigVersion"] == d["configVersion"]
    cfg_file = env["tmp"] / "fleets" / "lab-b" / "worker.yaml"
    assert cfg_file.exists() and (env["tmp"] / "fleets" / "lab-b" / "profiles" / "test-profile.yaml").exists()
    assert "LB-00001" in cfg_file.read_text()
    assert (await client.post(f"/api/v1/fleets/{f['fleetId']}/deploy")).status_code == 409

    # worker announces itself → running
    await _event(env, "worker_status", "", f["workerId"], {"status": "online", "vcp_count": 3})
    assert (await client.get(f"/api/v1/fleets/{f['fleetId']}")).json()["status"] == "running"

    # change → redeploy required
    await client.patch(f"/api/v1/fleets/{f['fleetId']}", json={"vcpCount": 4})
    assert (await client.get(f"/api/v1/fleets/{f['fleetId']}")).json()["redeployRequired"] is True
    assert (await client.delete(f"/api/v1/fleets/{f['fleetId']}")).status_code == 409

    # a deploy still inside the grace period is never judged by the monitor
    async with env["sf"]() as s:
        from vcp_control_plane.registry.store import VcpRegistryStore
        await VcpRegistryStore(s).update_fleet(f["fleetId"], runtime={})
        FakeLauncher.alive[f["fleetId"]] = False
        assert await fs.FleetService(VcpRegistryStore(s), env["settings"]).monitor_once() == []
        await VcpRegistryStore(s).update_fleet(f["fleetId"], runtime={"pid": 4242})

    # crash → monitor marks failed with the log tail
    FakeLauncher.alive[f["fleetId"]] = False
    async with env["sf"]() as s:
        from vcp_control_plane.registry.store import VcpRegistryStore
        failed = await fs.FleetService(VcpRegistryStore(s), env["settings"]).monitor_once()
    assert failed == [f["fleetId"]]
    fl = (await client.get(f"/api/v1/fleets/{f['fleetId']}")).json()
    assert fl["status"] == "failed" and "fake log line" in fl["lastError"]

    # redeploy → deploying with the new config, then stop
    r = await client.post(f"/api/v1/fleets/{f['fleetId']}/redeploy")
    assert r.json()["status"] == "deploying" and r.json()["redeployRequired"] is False
    r = await client.post(f"/api/v1/fleets/{f['fleetId']}/stop")
    assert r.json()["status"] == "stopped"
    v = (await client.get("/api/v1/vcps/LB-00001")).json()
    assert v["connectionState"] == "disconnected" and v["actualState"] == "offline"
    assert (await client.delete(f"/api/v1/fleets/{f['fleetId']}")).status_code == 200
    assert (await client.get("/api/v1/vcps/LB-00001")).status_code == 404


async def test_commands_catalogue(env):
    body = (await env["client"].get("/api/v1/commands")).json()
    assert body["contractVersion"] == "2"
    types = {c["commandType"] for c in body["items"]}
    assert types == set(contract.COMMAND_TYPES)


async def test_fleet_options_partial_and_config_defaults(env):
    client = env["client"]
    await client.post("/api/v1/profiles", json=PROFILE)
    r = await client.post("/api/v1/fleets", json={
        "name": "Opts", "csmsUrl": "ws://c", "profileId": "test-profile", "vcpCount": 1,
        "idPrefix": "OP-", "chargePointIdPrefix": "OPCP-", "options": {"bootJitterMs": [0, 10]}})
    assert r.status_code == 201
    assert r.json()["options"] == {"boot_jitter_ms": [0, 10]}           # no null keys stored
    cfg = (await client.get("/api/v1/fleets/opts/config")).json()
    assert cfg["redis"]["worker_heartbeat_s"] == 15.0
    assert cfg["redis"]["forward_traces_to_events"] is True and cfg["log_level"] == "INFO"


async def test_available_profiles_include_worker_dir(env, monkeypatch, tmp_path):
    d = tmp_path / "dp_profiles"
    d.mkdir()
    (d / "x.yaml").write_text("profile_id: dir_only_profile\nvendor: V\nmodel: M\n")
    monkeypatch.setattr(env["settings"], "worker_profiles_dir", str(d))
    await env["client"].post("/api/v1/profiles", json=PROFILE)
    items = (await env["client"].get("/api/v1/fleets/profiles")).json()
    src = {p["profileId"]: p["source"] for p in items}
    assert src == {"dir_only_profile": "worker_profiles_dir", "test-profile": "registry"}
