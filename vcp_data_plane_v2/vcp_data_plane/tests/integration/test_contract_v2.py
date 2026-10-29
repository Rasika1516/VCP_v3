"""Contract v2 integration tests: real WorkerRunner + mock CSMS + fakeredis.

Covers the four control-plane ↔ data-plane integration fixes:
  1. command envelope (command_json and legacy flat fields both accepted)
  2. command vocabulary (reset / change_state / unlock_connector / get_configuration)
  3. scenario catalogue (canonical IDs, v1 aliases, payload keys, new scenarios)
  4. events published to vcp:events:worker:{worker_id}
plus broadcast fan-out (per-worker consumer groups) and group filtering.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest
from prometheus_client import CollectorRegistry

from vcp_data_plane import contract
from vcp_data_plane.config.schema import WorkerConfig
from vcp_data_plane.devtools.mock_csms import MockCsms
from vcp_data_plane.worker.runner import WorkerRunner

PROFILES = str(Path(__file__).resolve().parents[2] / "configs" / "profiles")


class AsyncFakeRedis(fakeredis.aioredis.FakeRedis):
    """fakeredis implements BLOCK with a synchronous wait that freezes the event
    loop; emulate blocking reads with a short asyncio sleep instead."""

    async def xreadgroup(self, *args, block=None, **kwargs):
        res = await super().xreadgroup(*args, **kwargs)
        if not res and block:
            await asyncio.sleep(min(block, 50) / 1000)
        return res


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(worker_id: str, csms_url: str, vcps: list[dict[str, Any]]) -> WorkerConfig:
    return WorkerConfig.model_validate({
        "worker_id": worker_id,
        "csms": {"url": csms_url, "initial_backoff_s": 0.2, "max_backoff_s": 0.5},
        "vcps": vcps,
        "profile_dir": PROFILES,
        "boot_jitter_ms": [0, 0],
        "redis": {"stream_block_ms": 50, "worker_heartbeat_s": 0},
    })


def _vcp(n: int, group: str | None = None, profile: str = "generic_ac_ocpp16") -> dict[str, Any]:
    return {"vcp_id": f"VCP-{n:05d}", "charge_point_id": f"CP-{n:05d}", "profile_id": profile, "group_id": group}


async def _events(redis, worker_id: str) -> list[dict[str, Any]]:
    entries = await redis.xrange(f"vcp:events:worker:{worker_id}")
    out = []
    for _, f in entries:
        out.append({**f, "payload": json.loads(f["payload"])})
    return out


async def _wait_for(pred, timeout: float = 8.0, interval: float = 0.05):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        res = await pred()
        if res:
            return res
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


async def _send_v2(redis, stream: str, command_type: str, vcp_id: str | None = None,
                   payload: dict | None = None, group_id: str | None = None) -> str:
    cid = str(uuid.uuid4())
    cmd = {"command_id": cid, "command_type": command_type, "vcp_id": vcp_id,
           "group_id": group_id, "payload": payload or {}, "issued_by": "test", "issued_at": ""}
    await redis.xadd(stream, {"command_json": json.dumps(cmd), "command_type": command_type,
                              "vcp_id": vcp_id or ""})
    return cid


async def _result_for(redis, worker_id: str, cid: str, vcp_id: str | None = None):
    async def _p():
        for e in await _events(redis, worker_id):
            if e["event"] == "command_result" and e["payload"]["command_id"] == cid \
                    and (vcp_id is None or e["vcp_id"] == vcp_id):
                return e
        return None
    return await _wait_for(_p)


async def _vcp_booted(runner: WorkerRunner, vcp_id: str):
    from vcp_data_plane.state.machine import VcpState

    async def _p():
        return runner.runtimes[vcp_id].state.connection_state == VcpState.AVAILABLE
    await _wait_for(_p)


@pytest.fixture
async def env():
    redis = AsyncFakeRedis(decode_responses=True)
    csms = await MockCsms().start()
    runners: list[WorkerRunner] = []

    async def make(worker_id: str, vcps: list[dict[str, Any]]) -> WorkerRunner:
        r = WorkerRunner(_config(worker_id, csms.url, vcps), redis_client=redis,
                         metrics_registry=CollectorRegistry())
        await r.start()
        runners.append(r)
        return r

    yield redis, csms, make
    for r in runners:
        await r.stop()
    await csms.stop()
    await redis.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_worker_announces_itself_and_boots_vcps(env):
    redis, csms, make = env
    runner = await make("w-a", [_vcp(1, "g1"), _vcp(2)])
    await _vcp_booted(runner, "VCP-00001")
    await _vcp_booted(runner, "VCP-00002")

    events = await _events(redis, "w-a")
    online = [e for e in events if e["event"] == "worker_status" and e["payload"]["status"] == "online"]
    assert online and online[0]["payload"]["contract_version"] == contract.CONTRACT_VERSION
    assert {v["vcp_id"] for v in online[0]["payload"]["vcps"]} == {"VCP-00001", "VCP-00002"}
    assert online[0]["payload"]["vcps"][0]["group_id"] == "g1"

    # state_change events with the normalised vocabulary the control plane persists
    async def _connected():
        evs = [e for e in await _events(redis, "w-a")
               if e["event"] == "state_change" and e["vcp_id"] == "VCP-00001"]
        return evs and evs[-1]["payload"]["connection_state"] == "connected" \
            and evs[-1]["payload"]["actual_state"] == "idle"
    await _wait_for(_connected)

    # BootNotification sent once the socket is open (no 30 s boot-retry stall)
    assert "BootNotification" in csms.actions("CP-00001")

    # OCPP traces are forwarded as ocpp_trace events and written to vcp:trace:*
    events = await _events(redis, "w-a")
    traces = [e for e in events if e["event"] == "ocpp_trace" and e["vcp_id"] == "VCP-00001"]
    assert any(t["payload"]["action"] == "BootNotification" and t["payload"]["dir"] == "→" for t in traces)
    assert any(t["payload"]["action"] == "BootNotification.conf" for t in traces)
    assert await redis.xlen("vcp:trace:VCP-00001") > 0


async def test_command_json_envelope_executes_and_reports(env):
    redis, csms, make = env
    runner = await make("w-b", [_vcp(10)])
    await _vcp_booted(runner, "VCP-00010")

    cid = await _send_v2(redis, "vcp:commands:worker:w-b", "plug_ev", "VCP-00010", {"id_tag": "T1"})
    res = await _result_for(redis, "w-b", cid)
    assert res["payload"]["ok"] is True and res["vcp_id"] == "VCP-00010"
    assert runner.runtimes["VCP-00010"].state.primary.state.value == "Preparing"


async def test_legacy_flat_fields_still_accepted(env):
    redis, csms, make = env
    runner = await make("w-c", [_vcp(20)])
    await _vcp_booted(runner, "VCP-00020")
    cid = str(uuid.uuid4())
    await redis.xadd("vcp:commands:worker:w-c", {
        "command_id": cid, "command_type": "get_state", "vcp_id": "VCP-00020",
        "payload": "{}", "issued_by": "legacy", "group_id": "", "ts": "",
    })
    res = await _result_for(redis, "w-c", cid)
    assert res["payload"]["ok"] is True
    assert res["payload"]["result"]["state"]["vcp_id"] == "VCP-00020"


async def test_unknown_command_is_reported_not_silently_dropped(env):
    redis, csms, make = env
    runner = await make("w-d", [_vcp(30)])
    cid = await _send_v2(redis, "vcp:commands:worker:w-d", "teleport", "VCP-00030")
    res = await _result_for(redis, "w-d", cid)
    assert res["payload"]["ok"] is False
    assert "unsupported command_type" in res["payload"]["error"]


async def test_broadcast_reaches_every_worker_and_honours_group(env):
    redis, csms, make = env
    w1 = await make("w-e1", [_vcp(40, "alpha"), _vcp(41, "beta")])
    w2 = await make("w-e2", [_vcp(42, "alpha")])
    for w, v in ((w1, "VCP-00040"), (w1, "VCP-00041"), (w2, "VCP-00042")):
        await _vcp_booted(w, v)

    # fleet-wide: every VCP on every worker gets it
    cid = await _send_v2(redis, "vcp:commands:broadcast", "get_state")
    await _result_for(redis, "w-e1", cid, "VCP-00040")
    await _result_for(redis, "w-e1", cid, "VCP-00041")
    await _result_for(redis, "w-e2", cid, "VCP-00042")

    # group-scoped: only "alpha" VCPs, across both workers
    gid = await _send_v2(redis, "vcp:commands:broadcast", "get_state", group_id="alpha")
    await _result_for(redis, "w-e1", gid, "VCP-00040")
    await _result_for(redis, "w-e2", gid, "VCP-00042")
    await asyncio.sleep(0.3)
    beta = [e for e in await _events(redis, "w-e1")
            if e["event"] == "command_result" and e["payload"]["command_id"] == gid and e["vcp_id"] == "VCP-00041"]
    assert beta == []

    # targeted VCP on the broadcast stream: only the owner answers, no "not found" noise
    tid = await _send_v2(redis, "vcp:commands:broadcast", "get_state", vcp_id="VCP-00042")
    await _result_for(redis, "w-e2", tid, "VCP-00042")
    await asyncio.sleep(0.3)
    assert not [e for e in await _events(redis, "w-e1")
                if e["event"] == "command_result" and e["payload"]["command_id"] == tid]


async def test_operator_commands(env):
    redis, csms, make = env
    runner = await make("w-f", [_vcp(50)])
    await _vcp_booted(runner, "VCP-00050")
    stream = "vcp:commands:worker:w-f"

    cid = await _send_v2(redis, stream, "get_configuration", "VCP-00050")
    res = await _result_for(redis, "w-f", cid)
    keys = {k["key"] for k in res["payload"]["result"]["configuration"]["known"]}
    assert "HeartbeatInterval" in keys

    cid = await _send_v2(redis, stream, "change_state", "VCP-00050", {"desired_state": "Unavailable"})
    res = await _result_for(redis, "w-f", cid)
    assert res["payload"]["ok"] is True

    async def _unavail():
        evs = [e for e in await _events(redis, "w-f") if e["event"] == "state_change"]
        return evs and evs[-1]["payload"]["actual_state"] == "unavailable"
    await _wait_for(_unavail)

    cid = await _send_v2(redis, stream, "change_state", "VCP-00050", {"desired_state": "Available"})
    await _result_for(redis, "w-f", cid)
    assert runner.runtimes["VCP-00050"].state.primary.state.value == "Available"

    # unlock while charging: ends the session and unplugs
    for ct, p in (("plug_ev", {"id_tag": "T"}), ("authorize", {"id_tag": "T"}), ("start_charge", {})):
        await _result_for(redis, "w-f", await _send_v2(redis, stream, ct, "VCP-00050", p))
    assert runner.runtimes["VCP-00050"].state.primary.state.value == "Charging"
    res = await _result_for(redis, "w-f", await _send_v2(redis, stream, "unlock_connector", "VCP-00050",
                                                          {"connector_id": 1}))
    assert res["payload"]["ok"] is True
    assert "StopTransaction" in csms.actions("CP-00050")
    assert runner.runtimes["VCP-00050"].state.primary.state.value == "Available"

    # soft reset: drops the link and re-boots
    boots_before = csms.actions("CP-00050").count("BootNotification")
    res = await _result_for(redis, "w-f", await _send_v2(redis, stream, "reset", "VCP-00050", {"type": "Soft"}))
    assert res["payload"]["ok"] is True

    async def _rebooted():
        return csms.actions("CP-00050").count("BootNotification") > boots_before
    await _wait_for(_rebooted)


async def test_scenario_alias_with_execution_tracking(env):
    redis, csms, make = env
    runner = await make("w-g", [_vcp(60)])
    await _vcp_booted(runner, "VCP-00060")
    exec_id = str(uuid.uuid4())
    await _send_v2(redis, "vcp:commands:worker:w-g", "run_scenario", "VCP-00060",
                   {"scenario_id": "basic_charge_cycle", "execution_id": exec_id, "duration_s": 0.2})

    async def _done():
        evs = [e for e in await _events(redis, "w-g")
               if e["event"] == "scenario_status" and e["payload"]["execution_id"] == exec_id]
        return [e for e in evs if e["payload"]["status"] in ("completed", "failed")]
    final = (await _wait_for(_done))[-1]["payload"]
    assert final["status"] == "completed", final
    assert final["scenario_id"] == "plug_authorize_charge"
    assert final["requested_scenario_id"] == "basic_charge_cycle"
    acts = csms.actions("CP-00060")
    assert "StartTransaction" in acts and "StopTransaction" in acts


async def test_new_scenarios_boot_storm_and_meter_flood(env):
    redis, csms, make = env
    runner = await make("w-h", [_vcp(70), _vcp(71)])
    await _vcp_booted(runner, "VCP-00070")
    await _vcp_booted(runner, "VCP-00071")
    boots = csms.actions("CP-00070").count("BootNotification")

    sid = str(uuid.uuid4())
    await _send_v2(redis, "vcp:commands:worker:w-h", "run_scenario", "VCP-00070",
                   {"scenario_id": "boot_storm", "execution_id": sid, "jitter_ms": 100, "down_s": 0.1})
    fid = str(uuid.uuid4())
    await _send_v2(redis, "vcp:commands:worker:w-h", "run_scenario", "VCP-00071",
                   {"scenario_id": "meter_value_flood", "execution_id": fid, "duration_s": 0.5, "interval_s": 0.1})

    async def _final(eid):
        evs = [e["payload"] for e in await _events(redis, "w-h")
               if e["event"] == "scenario_status" and e["payload"]["execution_id"] == eid
               and e["payload"]["status"] in ("completed", "failed")]
        return evs[-1] if evs else None

    assert (await _wait_for(lambda: _final(sid)))["status"] == "completed"
    assert csms.actions("CP-00070").count("BootNotification") > boots
    assert (await _wait_for(lambda: _final(fid)))["status"] == "completed"
    assert csms.actions("CP-00071").count("MeterValues") >= 3
    meter = [e for e in await _events(redis, "w-h") if e["event"] == "meter_value" and e["vcp_id"] == "VCP-00071"]
    assert meter and {"energy_wh", "power_w"} <= set(meter[-1]["payload"])


async def test_trace_forwarding_switch(env):
    redis, csms, make = env
    cfg = _config("w-i", csms.url, [_vcp(80)])
    cfg.redis.forward_traces_to_events = False
    r = WorkerRunner(cfg, redis_client=redis, metrics_registry=CollectorRegistry())
    await r.start()
    try:
        await _vcp_booted(r, "VCP-00080")
        assert not [e for e in await _events(redis, "w-i") if e["event"] == "ocpp_trace"]
        assert await redis.xlen("vcp:trace:VCP-00080") > 0   # per-VCP trace stream still written
    finally:
        await r.stop()


async def test_worker_api_fleet_summary(env):
    from fastapi.testclient import TestClient
    from vcp_data_plane.api.worker_api import build_worker_api

    redis, csms, make = env
    runner = await make("w-j", [_vcp(90), _vcp(91)])
    app = build_worker_api("w-j", runner.registry, runner.dispatch_command, require_auth=False)
    body = TestClient(app).get("/fleet/summary").json()
    assert body["workerId"] == "w-j" and body["vcpCount"] == 2
    assert sum(body["connectorStateCounts"].values()) == 2
