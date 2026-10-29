"""VCP Fleet endpoints — define, configure and deploy data-plane workers.

A fleet is one data-plane worker plus the VCPs it simulates.  The control
plane generates the worker's ``worker.yaml`` from the fleet definition and
starts it as a local process or Docker container.

Routes
------
GET    /fleets                       list fleets (with VCP / connection counts)
POST   /fleets                       create a fleet (generates its VCPs)
GET    /fleets/launchers             which launchers are usable on this host
GET    /fleets/profiles              profiles a fleet can use (registry + worker_profiles_dir)
GET    /fleets/{id}                  fleet detail
PATCH  /fleets/{id}                  update definition (→ redeployRequired)
DELETE /fleets/{id}?force=           delete (must be stopped unless force)
GET    /fleets/{id}/config           generated worker.yaml (secrets masked)
GET    /fleets/{id}/vcps             VCPs in this fleet
POST   /fleets/{id}/deploy           write config + start worker
POST   /fleets/{id}/stop             stop worker
POST   /fleets/{id}/redeploy         stop + deploy with current definition
GET    /fleets/{id}/logs?tail=       worker log tail
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from vcp_control_plane.api.auth import make_auth_dependency
from vcp_control_plane.api.deps import DbSession, SettingsDep
from vcp_control_plane.api.schemas import (
    FleetCreateIn,
    FleetListOut,
    FleetOut,
    FleetPatchIn,
    OkOut,
    VcpListOut,
)
from vcp_control_plane.registry.store import VcpRegistryStore
from vcp_control_plane.workers.fleet_service import FleetError, FleetService
from vcp_control_plane.workers.launcher import LAUNCHERS, get_launcher

router = APIRouter()
_auth = make_auth_dependency()


def fleet_to_out(row, counts: dict[str, int] | None = None) -> FleetOut:
    counts = counts or {}
    auth = row.csms_auth or {}
    return FleetOut.model_validate({
        "fleetId": row.fleet_id,
        "name": row.name,
        "description": row.description or "",
        "workerId": row.worker_id,
        "csmsUrl": row.csms_url,
        "csmsAuthType": auth.get("type") or "none",
        "csmsUsername": auth.get("username"),
        "profileId": row.profile_id,
        "ocppVersion": row.ocpp_version,
        "groupId": row.group_id,
        "idPrefix": row.id_prefix,
        "chargePointIdPrefix": row.charge_point_id_prefix,
        "idStart": row.id_start,
        "options": row.options or {},
        "launcher": row.launcher,
        "status": row.status,
        "apiPort": row.api_port,
        "metricsPort": row.metrics_port,
        "workerUrl": row.worker_url or "",
        "configPath": row.config_path or "",
        "runtime": {k: v for k, v in (row.runtime or {}).items() if k != "command"},
        "lastError": row.last_error or "",
        "configVersion": row.config_version,
        "deployedConfigVersion": row.deployed_config_version,
        "redeployRequired": row.status in ("running", "deploying")
        and row.deployed_config_version != row.config_version,
        "vcpCount": counts.get("total", 0),
        "connectedCount": counts.get("connected", 0),
        "deployedAt": row.deployed_at,
        "stoppedAt": row.stopped_at,
        "lastSeen": row.last_seen,
        "createdAt": row.created_at,
    })


async def _out(store: VcpRegistryStore, row) -> FleetOut:
    counts = (await store.fleet_vcp_counts()).get(row.fleet_id, {})
    return fleet_to_out(row, counts)


def _raise(exc: FleetError):
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/fleets", response_model=FleetListOut, dependencies=[Depends(_auth)])
async def list_fleets(db: DbSession) -> FleetListOut:
    store = VcpRegistryStore(db)
    counts = await store.fleet_vcp_counts()
    rows = await store.list_fleets()
    return FleetListOut(items=[fleet_to_out(r, counts.get(r.fleet_id)) for r in rows], total=len(rows))


@router.get("/fleets/launchers", dependencies=[Depends(_auth)])
async def list_launchers(settings: SettingsDep) -> list[dict]:
    out = []
    for name in LAUNCHERS:
        ok, msg = await get_launcher(name, settings).check()
        out.append({"name": name, "available": ok, "detail": msg, "default": name == settings.default_launcher})
    return out


@router.get("/fleets/profiles", dependencies=[Depends(_auth)])
async def available_profiles(db: DbSession, settings: SettingsDep) -> list[dict]:
    """Profiles usable by a fleet: active registry profiles plus data-plane
    profile YAMLs found in VCP_CP_WORKER_PROFILES_DIR."""
    from vcp_control_plane.workers.config_gen import available_profile_ids

    rows = {p.profile_id: p for p in await VcpRegistryStore(db).list_profiles() if p.status == "active"}
    out = []
    for pid in available_profile_ids(settings, list(rows)):
        r = rows.get(pid)
        out.append({
            "profileId": pid,
            "source": "registry" if r else "worker_profiles_dir",
            "label": f"{r.vendor} {r.model} · OCPP {r.ocpp_version}" if r else "from worker profiles dir",
        })
    return out


@router.post("/fleets", response_model=FleetOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_auth)])
async def create_fleet(body: FleetCreateIn, db: DbSession, settings: SettingsDep) -> FleetOut:
    store = VcpRegistryStore(db)
    try:
        row = await FleetService(store, settings).create(body)
    except FleetError as exc:
        _raise(exc)
    return await _out(store, row)


@router.get("/fleets/{fleet_id}", response_model=FleetOut, dependencies=[Depends(_auth)])
async def get_fleet(fleet_id: str, db: DbSession, settings: SettingsDep) -> FleetOut:
    store = VcpRegistryStore(db)
    try:
        row = await FleetService(store, settings).get(fleet_id)
    except FleetError as exc:
        _raise(exc)
    return await _out(store, row)


@router.patch("/fleets/{fleet_id}", response_model=FleetOut, dependencies=[Depends(_auth)])
async def patch_fleet(fleet_id: str, body: FleetPatchIn, db: DbSession, settings: SettingsDep) -> FleetOut:
    store = VcpRegistryStore(db)
    try:
        row = await FleetService(store, settings).update(fleet_id, body)
    except FleetError as exc:
        _raise(exc)
    return await _out(store, row)


@router.delete("/fleets/{fleet_id}", response_model=OkOut, dependencies=[Depends(_auth)])
async def delete_fleet(fleet_id: str, db: DbSession, settings: SettingsDep,
                       force: bool = Query(default=False)) -> OkOut:
    try:
        await FleetService(VcpRegistryStore(db), settings).delete(fleet_id, force=force)
    except FleetError as exc:
        _raise(exc)
    return OkOut(message=f"Fleet '{fleet_id}' deleted")


@router.get("/fleets/{fleet_id}/config", dependencies=[Depends(_auth)])
async def fleet_config(fleet_id: str, db: DbSession, settings: SettingsDep,
                       format: str = Query(default="json")):
    import yaml

    try:
        cfg = await FleetService(VcpRegistryStore(db), settings).preview_config(fleet_id)
    except FleetError as exc:
        _raise(exc)
    if format == "yaml":
        return PlainTextResponse(yaml.safe_dump(cfg, sort_keys=False), media_type="text/yaml")
    return cfg


@router.get("/fleets/{fleet_id}/vcps", response_model=VcpListOut, dependencies=[Depends(_auth)])
async def fleet_vcps(fleet_id: str, db: DbSession,
                     page: int = Query(default=1, ge=1),
                     page_size: int = Query(default=100, ge=1, le=1000, alias="pageSize")) -> VcpListOut:
    from vcp_control_plane.api.routers.vcps import _to_vcp_out

    store = VcpRegistryStore(db)
    rows, total = await store.list_vcps(page=page, page_size=page_size, fleet_id=fleet_id)
    return VcpListOut(items=[_to_vcp_out(r) for r in rows], total=total, page=page, **{"pageSize": page_size})


@router.post("/fleets/{fleet_id}/deploy", response_model=FleetOut, dependencies=[Depends(_auth)])
async def deploy_fleet(fleet_id: str, db: DbSession, settings: SettingsDep) -> FleetOut:
    store = VcpRegistryStore(db)
    try:
        row = await FleetService(store, settings).deploy(fleet_id)
    except FleetError as exc:
        _raise(exc)
    return await _out(store, row)


@router.post("/fleets/{fleet_id}/stop", response_model=FleetOut, dependencies=[Depends(_auth)])
async def stop_fleet(fleet_id: str, db: DbSession, settings: SettingsDep) -> FleetOut:
    store = VcpRegistryStore(db)
    try:
        row = await FleetService(store, settings).stop(fleet_id)
    except FleetError as exc:
        _raise(exc)
    return await _out(store, row)


@router.post("/fleets/{fleet_id}/redeploy", response_model=FleetOut, dependencies=[Depends(_auth)])
async def redeploy_fleet(fleet_id: str, db: DbSession, settings: SettingsDep) -> FleetOut:
    store = VcpRegistryStore(db)
    try:
        row = await FleetService(store, settings).redeploy(fleet_id)
    except FleetError as exc:
        _raise(exc)
    return await _out(store, row)


@router.get("/fleets/{fleet_id}/logs", response_class=PlainTextResponse, dependencies=[Depends(_auth)])
async def fleet_logs(fleet_id: str, db: DbSession, settings: SettingsDep,
                     tail: int = Query(default=200, ge=1, le=5000)) -> str:
    try:
        return await FleetService(VcpRegistryStore(db), settings).logs(fleet_id, tail)
    except FleetError as exc:
        _raise(exc)
