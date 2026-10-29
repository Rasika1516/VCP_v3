"""Scenario execution endpoints.

The catalogue (bus/contract.py SCENARIO_CATALOGUE) is shared with
vcp_data_plane: 10 scenarios — the worker's original 8 plus boot_storm and
meter_value_flood.  The v1 IDs basic_charge_cycle, rapid_reconnect and
remote_start_stop are accepted as aliases.

Each run creates a test_execution row; its execution_id travels in the
run_scenario payload and workers report per-VCP progress as scenario_status
events, which close the execution with result passed | failed | partial.

Routes
------
GET  /scenarios               — list available built-in scenarios
POST /scenarios/run           — launch an execution
GET  /scenarios/executions    — list past executions
GET  /scenarios/executions/{id} — execution detail (with per-VCP report)
POST /scenarios/executions/{id}/cancel — cancel a running execution
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, RedisClient, SettingsDep
from vcp_control_plane.api.schemas import (
    ExecutionListOut,
    ExecutionOut,
    OkOut,
    ScenarioRunIn,
)
from vcp_control_plane.bus import contract
from vcp_control_plane.bus.publisher import CommandPublisher
from vcp_control_plane.registry.store import VcpRegistryStore

router = APIRouter()
_auth = make_auth_dependency()

def live_worker_ids(max_age_s: float = 60.0) -> set[str]:
    from datetime import datetime, timezone

    from vcp_control_plane.workers.directory import get_worker_directory

    now = datetime.now(timezone.utc)
    return {
        w.worker_id for w in get_worker_directory().all()
        if w.status == "online" and w.last_seen and (now - w.last_seen).total_seconds() <= max_age_s
    }


# Kept for backwards compatibility with v1 imports
BUILT_IN_SCENARIOS = contract.SCENARIO_CATALOGUE
_SCENARIO_IDS = set(contract.SCENARIO_IDS) | set(contract.SCENARIO_ALIASES)


def _exec_to_out(row, include_report: bool = False) -> ExecutionOut:
    report = row.report or {}
    return ExecutionOut.model_validate(
        {
            "progress": report.get("counts", {}),
            "report": report if include_report else None,
            "executionId": row.execution_id,
            "scenarioId": row.scenario_id,
            "scenarioName": row.scenario_name,
            "targetScope": row.target_scope,
            "status": row.status,
            "result": row.result,
            "startTime": row.start_time,
            "endTime": row.end_time,
            "targetCount": row.target_count,
            "createdAt": row.created_at,
        }
    )


@router.get("/scenarios", dependencies=[Depends(_auth)])
async def list_scenarios() -> list[dict]:
    return contract.SCENARIO_CATALOGUE


@router.post("/scenarios/run", response_model=ExecutionOut,
             status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_auth)])
async def run_scenario(
    body: ScenarioRunIn,
    db: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> ExecutionOut:
    canonical, params = contract.resolve_scenario(body.scenario_id, body.parameters)
    entry = contract.scenario_entry(canonical)
    if entry is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scenario '{body.scenario_id}'. Available: {sorted(contract.SCENARIO_IDS)}",
        )

    store = VcpRegistryStore(db)
    publisher = CommandPublisher(redis_client=redis, settings=settings)
    execution_id = str(uuid.uuid4())
    payload = {"scenario_id": canonical, "execution_id": execution_id, **params}

    # Resolve targets first so the execution knows how many results to expect
    if body.target_scope == "single":
        if not body.target_vcp_id:
            raise HTTPException(status_code=422, detail="targetVcpId is required for scope 'single'")
        vcp = await store.get_vcp(body.target_vcp_id)
        if vcp is None:
            raise HTTPException(status_code=404, detail=f"VCP '{body.target_vcp_id}' not found")
        targets = [vcp]
    elif body.target_scope == "group":
        if not body.target_group_id:
            raise HTTPException(status_code=422, detail="targetGroupId is required for scope 'group'")
        targets, _ = await store.list_vcps(group_id=body.target_group_id, page=1, page_size=100_000)
        if not targets:
            raise HTTPException(status_code=404, detail=f"No VCPs in group '{body.target_group_id}'")
    else:
        targets = None  # fleet

    # Expect results only from VCPs whose worker is live (online in the worker
    # directory); otherwise an undeployed fleet would keep the execution open.
    live = live_worker_ids()
    candidates = targets if targets is not None else (await store.list_vcps(page=1, page_size=100_000))[0]
    live_targets = [v for v in candidates if v.worker_id in live]
    target_count = len(live_targets) or len(candidates)
    row = await store.create_execution(
        execution_id=execution_id,
        scenario_id=canonical,
        scenario_name=body.scenario_name or entry["name"],
        target_scope=body.target_scope if body.target_scope in ("single", "group") else "fleet",
        target_vcp_id=body.target_vcp_id,
        target_group_id=body.target_group_id,
        target_count=target_count,
        initiated_by=body.initiated_by,
    )

    if targets is None:
        await publisher.broadcast(
            command_type="run_scenario", payload=payload,
            command_id=str(uuid.uuid4()), issued_by=body.initiated_by,
        )
    else:
        for vcp in targets:
            await publisher.send_to_vcp(
                vcp_id=vcp.vcp_id, worker_id=vcp.worker_id, command_type="run_scenario",
                payload=payload, command_id=str(uuid.uuid4()), issued_by=body.initiated_by,
                group_id=vcp.group_id,
            )
    return _exec_to_out(row)


@router.get("/scenarios/executions", response_model=ExecutionListOut, dependencies=[Depends(_auth)])
async def list_executions(db: DbSession) -> ExecutionListOut:
    store = VcpRegistryStore(db)
    rows = await store.list_executions()
    return ExecutionListOut(items=[_exec_to_out(r) for r in rows], total=len(rows))


@router.get("/scenarios/executions/{execution_id}", response_model=ExecutionOut,
            dependencies=[Depends(_auth)])
async def get_execution(execution_id: str, db: DbSession) -> ExecutionOut:
    store = VcpRegistryStore(db)
    row = await store.get_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return _exec_to_out(row, include_report=True)


@router.post("/scenarios/executions/{execution_id}/cancel", response_model=OkOut,
             dependencies=[Depends(_auth)])
async def cancel_execution(execution_id: str, db: DbSession, redis: RedisClient,
                           settings: SettingsDep) -> OkOut:
    store = VcpRegistryStore(db)
    row = await store.get_execution(execution_id)
    ok = await store.cancel_execution(execution_id)
    if not ok or row is None:
        raise HTTPException(status_code=404,
                            detail=f"Execution '{execution_id}' not found or already finished")
    # Stop the scenario on the targeted VCPs
    publisher = CommandPublisher(redis_client=redis, settings=settings)
    cid = str(uuid.uuid4())
    if row.target_scope == "single" and row.target_vcp_id:
        vcp = await store.get_vcp(row.target_vcp_id)
        await publisher.send_to_vcp(row.target_vcp_id, vcp.worker_id if vcp else None,
                                    "stop_scenario", {}, cid, issued_by="cancel")
    else:
        await publisher.broadcast("stop_scenario", {}, cid, issued_by="cancel",
                                  group_id=row.target_group_id if row.target_scope == "group" else None)
    return OkOut(message=f"Execution '{execution_id}' cancelled")
