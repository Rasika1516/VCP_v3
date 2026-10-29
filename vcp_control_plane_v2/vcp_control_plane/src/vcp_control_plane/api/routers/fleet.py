"""Fleet-level summary and bulk command endpoints.

Routes
------
GET  /fleet/summary          — aggregate counts across all workers
POST /fleet/commands         — broadcast, group-scoped or VCP-list bulk command
GET  /fleet/workers          — known workers + reachability

Workers come from the WorkerDirectory: static configuration (VCP_CP_WORKERS /
VCP_CP_WORKER_URLS), fleets deployed from the UI, and workers discovered on
the event bus.  Each worker with a URL is polled at ``GET {url}/fleet/summary``.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import APIRouter, Depends

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, RedisClient, SettingsDep
from vcp_control_plane.api.routers.vcps import validate_command_type
from vcp_control_plane.api.schemas import (
    BulkCommandIn,
    BulkCommandOut,
    FleetSummaryOut,
    ShardSummaryOut,
)
from vcp_control_plane.bus.publisher import CommandPublisher
from vcp_control_plane.registry.store import VcpRegistryStore
from vcp_control_plane.workers.directory import get_worker_directory

router = APIRouter()
_auth = make_auth_dependency()


async def _query_worker(base_url: str, token: str | None) -> dict | None:
    if not base_url:
        return None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=3.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/fleet/summary")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


async def _workers_with_status(db, settings) -> list[dict]:
    directory = get_worker_directory()
    store = VcpRegistryStore(db)
    await directory.refresh(settings, fleets=await store.list_fleets())
    workers = directory.all()
    results = await asyncio.gather(*[_query_worker(w.url, settings.worker_token) for w in workers])
    out = []
    for w, res in zip(workers, results):
        d = w.to_dict()
        d["reachable"] = res is not None
        if res is not None:
            d["vcpCount"] = res.get("vcpCount", d["vcpCount"])
            d["connectorStateCounts"] = res.get("connectorStateCounts", {})
            d["connectionStateCounts"] = res.get("connectionStateCounts", {})
        else:
            d["connectorStateCounts"] = w.state_distribution or {}
            d["connectionStateCounts"] = {}
        out.append(d)
    return out


@router.get("/fleet/summary", response_model=FleetSummaryOut, dependencies=[Depends(_auth)])
async def fleet_summary(db: DbSession, settings: SettingsDep) -> FleetSummaryOut:
    store = VcpRegistryStore(db)
    counts = await store.aggregate_state_counts()
    workers = await _workers_with_status(db, settings)
    shards = [
        ShardSummaryOut.model_validate({
            "shardIndex": i,
            "workerId": w["workerId"],
            "reachable": w["reachable"],
            "vcpCount": w["vcpCount"],
            "connectorStateCounts": w["connectorStateCounts"],
        })
        for i, w in enumerate(workers)
    ]
    return FleetSummaryOut.model_validate({
        "vcpCount": counts["total"],
        "connectionStateCounts": counts["connection_states"],
        "actualStateCounts": counts["actual_states"],
        "workerCount": len(workers),
        "shards": [s.model_dump(by_alias=True) for s in shards],
    })


@router.post("/fleet/commands", response_model=BulkCommandOut, dependencies=[Depends(_auth)])
async def bulk_command(
    body: BulkCommandIn,
    db: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> BulkCommandOut:
    """Dispatch a command to a list of VCPs, a group, or the whole fleet."""
    validate_command_type(body.command_type)
    store = VcpRegistryStore(db)
    publisher = CommandPublisher(redis_client=redis, settings=settings)
    command_ids: list[str] = []

    if body.vcp_ids:
        for vcp_id in body.vcp_ids:
            row = await store.get_vcp(vcp_id)
            cid = str(uuid.uuid4())
            # audit first — results can arrive before the publish returns
            await store.record_command(
                command_id=cid, command_type=body.command_type,
                vcp_id=vcp_id, payload=body.payload, issued_by=body.issued_by,
            )
            await publisher.send_to_vcp(
                vcp_id=vcp_id,
                worker_id=row.worker_id if row else None,
                command_type=body.command_type,
                payload=body.payload,
                command_id=cid,
                issued_by=body.issued_by,
            )
            command_ids.append(cid)
    else:
        # Group-scoped (group_id set) or fleet-wide broadcast.  Every worker
        # reads the broadcast stream via its own consumer group and executes
        # the command on its VCPs (filtered by group_id when present).
        cid = str(uuid.uuid4())
        await store.record_command(
            command_id=cid, command_type=body.command_type,
            vcp_id=None, payload=body.payload, issued_by=body.issued_by,
            group_id=body.group_id,
        )
        await publisher.broadcast(
            command_type=body.command_type,
            payload=body.payload,
            command_id=cid,
            issued_by=body.issued_by,
            group_id=body.group_id,
        )
        command_ids.append(cid)

    return BulkCommandOut.model_validate({"dispatched": len(command_ids), "commandIds": command_ids})


@router.get("/fleet/workers", dependencies=[Depends(_auth)])
async def list_workers(db: DbSession, settings: SettingsDep) -> list[dict]:
    """Known workers with reachability and live shard counts."""
    return await _workers_with_status(db, settings)
