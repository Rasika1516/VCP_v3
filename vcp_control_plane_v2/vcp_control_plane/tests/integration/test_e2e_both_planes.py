"""End-to-end: Control Plane API + EventSubscriber  ⇄  Redis  ⇄  Data Plane worker  ⇄  mock CSMS.

Exercises the four v1 integration gaps through the real code on both sides:
commands published by the control plane are executed by the worker, the
worker's events update the registry, scenario executions close, and the
worker config generated from a fleet is the one the worker runs with.

Skipped when vcp_data_plane is not installed alongside the control plane.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytest.importorskip("vcp_data_plane")

import fakeredis.aioredis  # noqa: E402
from prometheus_client import CollectorRegistry  # noqa: E402
from vcp_data_plane.config.schema import WorkerConfig  # noqa: E402
from vcp_data_plane.devtools.mock_csms import MockCsms  # noqa: E402
from vcp_data_plane.worker.runner import WorkerRunner  # noqa: E402

from vcp_control_plane.db.models import Base  # noqa: E402
from vcp_control_plane.workers.directory import WorkerDirectory  # noqa: E402

import vcp_data_plane  # noqa: E402

DP_PROFILES = Path(vcp_data_plane.__file__).resolve().parents[2] / "configs" / "profiles"


class AsyncFakeRedis(fakeredis.aioredis.FakeRedis):
    """fakeredis blocks the event loop on XREADGROUP BLOCK; emulate it."""

    async def xreadgroup(self, *args, block=None, **kwargs):
        res = await super().xreadgroup(*args, **kwargs)
        if not res and block:
            await asyncio.sleep(min(block, 50) / 1000)
        return res


async def wait_for(pred, timeout=10.0, interval=0.1):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    last = None
    while loop.time() < end:
        last = await pred()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


@pytest_asyncio.fixture()
async def stack(tmp_path, monkeypatch):
    from vcp_control_plane.api.app import create_app
    from vcp_control_plane.api.deps import get_redis
    from vcp_control_plane.bus.subscriber import EventSubscriber
    from vcp_control_plane.config.settings import get_settings
    from vcp_control_plane.db.base import get_db_session
    import vcp_control_plane.workers.directory as dirmod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e2e.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    redis = AsyncFakeRedis(decode_responses=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "fleet_config_dir", str(tmp_path / "fleets"))
    monkeypatch.setattr(settings, "worker_profiles_dir", str(DP_PROFILES))
    monkeypatch.setattr(settings, "stream_block_ms", 50)
    monkeypatch.setattr(settings, "event_stream_refresh_s", 0.2)
    directory = WorkerDirectory()
    monkeypatch.setattr(dirmod, "_directory", directory)

    app = create_app()

    async def override_db():
        async with sf() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_redis] = lambda: redis
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    sub = EventSubscriber(redis, settings, session_factory=sf, directory=directory, broadcast=capture)
    sub_task = asyncio.create_task(sub.run())
    csms = await MockCsms().start()
    runners: list[WorkerRunner] = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"Authorization": "Bearer test-token"}) as client:
        yield {"client": client, "redis": redis, "csms": csms, "frames": frames,
               "settings": settings, "runners": runners, "sf": sf}

    for r in runners:
        await r.stop()
    sub.stop()
    sub_task.cancel()
    await asyncio.gather(sub_task, return_exceptions=True)
    await csms.stop()
    await redis.aclose()
    await engine.dispose()


async def _deploy_in_process(stack, fleet_id: str) -> WorkerRunner:
    """Run the worker from the control plane's generated config, in-process."""
    client = stack["client"]
    cfg = (await client.get(f"/api/v1/fleets/{fleet_id}/config")).json()
    cfg["csms"]["url"] = stack["csms"].url
    cfg["boot_jitter_ms"] = [0, 0]
    cfg["redis"]["stream_block_ms"] = 50
    cfg["profile_dir"] = str(DP_PROFILES)
    async with stack["sf"]() as s:
        from vcp_control_plane.registry.store import VcpRegistryStore
        await VcpRegistryStore(s).update_fleet(fleet_id, status="deploying")
    runner = WorkerRunner(WorkerConfig.model_validate(cfg), redis_client=stack["redis"],
                          metrics_registry=CollectorRegistry())
    await runner.start()
    stack["runners"].append(runner)
    return runner


async def test_full_round_trip(stack):
    client = stack["client"]
    r = await client.post("/api/v1/fleets", json={
        "name": "E2E", "csmsUrl": "ws://placeholder", "profileId": "generic_ac_ocpp16",
        "vcpCount": 3, "idPrefix": "E2E-", "chargePointIdPrefix": "E2ECP-", "groupId": "grp-e2e",
    })
    assert r.status_code == 201, r.text
    fleet = r.json()
    await _deploy_in_process(stack, fleet["fleetId"])

    # 4. events: worker online → fleet running; VCPs connected + idle in the registry
    async def _running():
        return (await client.get(f"/api/v1/fleets/{fleet['fleetId']}")).json()["status"] == "running"
    await wait_for(_running)

    async def _all_idle():
        items = (await client.get("/api/v1/vcps", params={"fleetId": fleet["fleetId"]})).json()["items"]
        return len(items) == 3 and all(v["connectionState"] == "connected" and v["actualState"] == "idle"
                                       for v in items)
    await wait_for(_all_idle)

    # 1. command envelope: per-VCP command executed + acknowledged
    r = await client.post("/api/v1/vcps/E2E-00001/commands",
                          json={"commandType": "plug_ev", "payload": {"id_tag": "E2E"}})
    cid = r.json()["commandId"]

    async def _acked():
        audit = (await client.get("/api/v1/vcps/E2E-00001/commands")).json()
        return next((a for a in audit if a["commandId"] == cid and a["status"] == "acknowledged"), None)
    await wait_for(_acked)
    assert (await client.get("/api/v1/vcps/E2E-00001")).json()["actualState"] == "preparing"

    # 2. operator command vocabulary via a group broadcast
    r = await client.post("/api/v1/fleet/commands", json={
        "commandType": "change_state", "groupId": "grp-e2e", "payload": {"desired_state": "Unavailable"}})
    assert r.status_code == 200

    async def _unavailable():
        items = (await client.get("/api/v1/vcps", params={"groupId": "grp-e2e"})).json()["items"]
        return sum(v["actualState"] == "unavailable" for v in items) >= 2
    await wait_for(_unavailable)
    await client.post("/api/v1/fleet/commands", json={
        "commandType": "change_state", "groupId": "grp-e2e", "payload": {"desired_state": "Available"}})
    await client.post("/api/v1/vcps/E2E-00001/commands", json={"commandType": "unplug_ev"})

    async def _idle_again():
        items = (await client.get("/api/v1/vcps", params={"fleetId": fleet["fleetId"]})).json()["items"]
        return all(v["actualState"] == "idle" for v in items)
    await wait_for(_idle_again)

    # 3. scenario alias over the group → execution closes as passed
    r = await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "basic_charge_cycle", "targetScope": "group", "targetGroupId": "grp-e2e",
        "parameters": {"duration_s": 0.3}})
    assert r.status_code == 202, r.text
    eid = r.json()["executionId"]

    async def _exec_done():
        ex = (await client.get(f"/api/v1/scenarios/executions/{eid}")).json()
        return ex if ex["status"] in ("completed", "failed") else None
    ex = await wait_for(_exec_done, timeout=15)
    assert ex["result"] == "passed", ex
    assert ex["progress"]["completed"] == 3
    assert stack["csms"].actions("E2ECP-00002").count("StartTransaction") == 1

    # live events reached WebSocket subscribers, including OCPP traces
    kinds = {f["event"] for f in stack["frames"]}
    assert {"worker_status", "state_change", "ocpp_trace", "command_result", "scenario_status"} <= kinds

    # fleet-wide new scenario: boot_storm re-boots every VCP
    boots = len([a for a in stack["csms"].actions() if a == "BootNotification"])
    r = await client.post("/api/v1/scenarios/run", json={
        "scenarioId": "boot_storm", "targetScope": "fleet", "parameters": {"down_s": 0.1, "jitter_ms": 50}})
    eid = r.json()["executionId"]
    ex = await wait_for(_exec_done, timeout=15)
    assert ex["result"] == "passed", ex
    assert len([a for a in stack["csms"].actions() if a == "BootNotification"]) >= boots + 3
