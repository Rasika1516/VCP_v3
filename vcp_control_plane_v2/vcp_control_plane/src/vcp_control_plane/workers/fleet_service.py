"""VCP Fleet service — definition → generated worker config → running worker.

Lifecycle
---------
  create   validate + generate N VCP rows (``{id_prefix}{n:05d}``) + allocate ports
  update   CSMS / profile / group / count / launcher / options; bumps
           ``config_version`` so the UI can flag "redeploy required"
  deploy   write ``worker.yaml`` + profiles, start the worker via its launcher,
           status → deploying; the EventSubscriber flips it to running when the
           worker publishes ``worker_status: online``
  stop     stop the worker, status → stopped, fleet VCPs marked offline
  redeploy stop (if running) + deploy with the current definition
  delete   only when not running; removes the fleet and its VCPs

``monitor_once`` (run periodically from the app lifespan) marks fleets whose
worker process/container died as ``failed`` and records the reason.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update

from vcp_control_plane.config.settings import Settings
from vcp_control_plane.db.models import VcpInstance
from vcp_control_plane.registry.store import VcpRegistryStore
from vcp_control_plane.workers.config_gen import (
    FleetConfigError,
    build_worker_config,
    fleet_dir,
    write_fleet_files,
)
from vcp_control_plane.workers.directory import WorkerDirectory, get_worker_directory
from vcp_control_plane.workers.launcher import LAUNCHERS, LauncherError, get_launcher

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("deploying", "running", "stopping")
MAX_VCPS_PER_FLEET = 5000
MONITOR_GRACE_S = 3.0
_SLUG = re.compile(r"[^a-z0-9-]+")


class FleetError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(text: str) -> str:
    s = _SLUG.sub("-", text.strip().lower()).strip("-")
    return s[:48] or "fleet"


def vcp_ids_for(prefix: str, cp_prefix: str, start: int, count: int) -> list[tuple[str, str]]:
    return [(f"{prefix}{n:05d}", f"{cp_prefix}{n:05d}") for n in range(start, start + count)]


class FleetService:
    def __init__(self, store: VcpRegistryStore, settings: Settings,
                 directory: WorkerDirectory | None = None) -> None:
        self.store = store
        self.settings = settings
        self.directory = directory or get_worker_directory()

    # ------------------------------------------------------------------
    # Create / update / delete
    # ------------------------------------------------------------------

    async def _allocate_ports(self) -> tuple[int, int]:
        used_api, used_metrics = await self.store.used_ports()
        api = self.settings.worker_api_port_start
        while api in used_api:
            api += 1
        met = self.settings.worker_metrics_port_start
        while met in used_metrics:
            met += 1
        return api, met

    async def _check_profile(self, profile_id: str) -> None:
        from vcp_control_plane.workers.config_gen import available_profile_ids
        registry_ids = [p.profile_id for p in await self.store.list_profiles() if p.status == "active"]
        if profile_id not in available_profile_ids(self.settings, registry_ids):
            raise FleetError(f"profile '{profile_id}' not found — import it on the Profiles page first", 422)

    async def create(self, body: Any) -> Any:
        fleet_id = slugify(body.fleet_id or body.name)
        if await self.store.get_fleet(fleet_id):
            raise FleetError(f"fleet '{fleet_id}' already exists", 409)
        worker_id = body.worker_id or f"fleet-{fleet_id}"
        if await self.store.get_fleet_by_worker(worker_id):
            raise FleetError(f"worker id '{worker_id}' is already used by another fleet", 409)
        if body.launcher not in LAUNCHERS:
            raise FleetError(f"launcher must be one of {LAUNCHERS}", 422)
        if not 1 <= body.vcp_count <= MAX_VCPS_PER_FLEET:
            raise FleetError(f"vcpCount must be between 1 and {MAX_VCPS_PER_FLEET}", 422)
        await self._check_profile(body.profile_id)

        pairs = vcp_ids_for(body.id_prefix, body.charge_point_id_prefix, body.id_start, body.vcp_count)
        await self._check_id_clashes(pairs)

        api_port, metrics_port = await self._allocate_ports()
        fleet = await self.store.create_fleet(
            fleet_id=fleet_id,
            name=body.name,
            description=body.description,
            worker_id=worker_id,
            csms_url=body.csms_url,
            csms_auth=body.csms_auth.model_dump() if body.csms_auth else {},
            profile_id=body.profile_id,
            ocpp_version=body.ocpp_version,
            group_id=body.group_id,
            id_prefix=body.id_prefix,
            charge_point_id_prefix=body.charge_point_id_prefix,
            id_start=body.id_start,
            options=body.options.model_dump(exclude_none=True) if body.options else {},
            launcher=body.launcher,
            status="draft",
            api_port=api_port,
            metrics_port=metrics_port,
            config_version=1,
            deployed_config_version=0,
            runtime={},
        )
        await self._add_generated(fleet, pairs)
        return fleet

    async def _check_id_clashes(self, pairs: list[tuple[str, str]]) -> None:
        clash_v = await self.store.vcp_ids_exist([p[0] for p in pairs])
        clash_c = await self.store.charge_point_ids_exist([p[1] for p in pairs])
        if clash_v or clash_c:
            sample = sorted(clash_v | clash_c)[:5]
            raise FleetError(
                f"{len(clash_v | clash_c)} VCP / charge point IDs already exist (e.g. {', '.join(sample)}) — "
                f"choose another ID prefix or start index", 409,
            )

    async def _add_generated(self, fleet: Any, pairs: list[tuple[str, str]]) -> None:
        await self.store.add_vcps([
            {
                "vcp_id": vid,
                "charge_point_id": cpid,
                "profile_id": fleet.profile_id,
                "ocpp_version": fleet.ocpp_version or "1.6J",
                "group_id": fleet.group_id,
                "fleet_id": fleet.fleet_id,
                "worker_id": fleet.worker_id,
                "meta": {"generated": True},
            }
            for vid, cpid in pairs
        ])

    async def update(self, fleet_id: str, body: Any) -> Any:
        fleet = await self.get(fleet_id)
        values: dict[str, Any] = {}
        for attr in ("name", "description", "csms_url", "ocpp_version", "group_id"):
            v = getattr(body, attr, None)
            if v is not None:
                values[attr] = v
        if body.csms_auth is not None:
            auth = body.csms_auth.model_dump()
            prev = fleet.csms_auth or {}
            # blank password on edit = keep the stored one
            if auth.get("type") == "basic" and not auth.get("password") and prev.get("password"):
                auth["password"] = prev["password"]
            if auth.get("type") != "basic":
                auth = {"type": auth.get("type") or "none"}
            values["csms_auth"] = auth
        if body.options is not None:
            values["options"] = {**(fleet.options or {}), **body.options.model_dump(exclude_none=True)}
        if body.launcher is not None:
            if body.launcher not in LAUNCHERS:
                raise FleetError(f"launcher must be one of {LAUNCHERS}", 422)
            if fleet.status in ACTIVE_STATUSES and body.launcher != fleet.launcher:
                raise FleetError("stop the fleet before changing its launcher", 409)
            values["launcher"] = body.launcher
        if body.profile_id is not None and body.profile_id != fleet.profile_id:
            await self._check_profile(body.profile_id)
            values["profile_id"] = body.profile_id
            await self.store._db.execute(  # noqa: SLF001
                update(VcpInstance)
                .where(VcpInstance.fleet_id == fleet_id, VcpInstance.profile_id == fleet.profile_id)
                .values(profile_id=body.profile_id)
            )
        if "group_id" in values and values["group_id"] != fleet.group_id:
            await self.store._db.execute(  # noqa: SLF001
                update(VcpInstance)
                .where(VcpInstance.fleet_id == fleet_id, VcpInstance.group_id == fleet.group_id)
                .values(group_id=values["group_id"] or None)
            )
        if "ocpp_version" in values and values["ocpp_version"]:
            await self.store._db.execute(  # noqa: SLF001
                update(VcpInstance).where(VcpInstance.fleet_id == fleet_id)
                .values(ocpp_version=values["ocpp_version"])
            )

        if body.vcp_count is not None:
            await self._resize(fleet, body.vcp_count)

        config_fields = set(values) - {"name", "description", "launcher"}
        if values:
            fleet = await self.store.update_fleet(fleet_id, **values)
        if config_fields or body.vcp_count is not None:
            await self.store.bump_fleet_config(fleet_id)
        return await self.get(fleet_id)

    async def _resize(self, fleet: Any, count: int) -> None:
        if not 1 <= count <= MAX_VCPS_PER_FLEET:
            raise FleetError(f"vcpCount must be between 1 and {MAX_VCPS_PER_FLEET}", 422)
        vcps = await self.store.fleet_vcps(fleet.fleet_id)
        generated = [v for v in vcps if (v.meta or {}).get("generated")]
        wanted = vcp_ids_for(fleet.id_prefix, fleet.charge_point_id_prefix, fleet.id_start, count)
        wanted_ids = {p[0] for p in wanted}
        have_ids = {v.vcp_id for v in generated}
        for v in generated:
            if v.vcp_id not in wanted_ids:
                await self.store.delete_vcp(v.vcp_id)
        missing = [p for p in wanted if p[0] not in have_ids]
        if missing:
            await self._check_id_clashes(missing)
            await self._add_generated(fleet, missing)

    async def add_vcp(self, fleet_id: str, body: Any) -> Any:
        """Register one VCP into a fleet (Register VCP form)."""
        fleet = await self.get(fleet_id)
        vcp_id = body.vcp_id
        if not vcp_id:
            existing = {v.vcp_id for v in await self.store.fleet_vcps(fleet_id)}
            n = fleet.id_start
            while f"{fleet.id_prefix}{n:05d}" in existing or await self.store.get_vcp(f"{fleet.id_prefix}{n:05d}"):
                n += 1
            vcp_id = f"{fleet.id_prefix}{n:05d}"
        if await self.store.get_vcp(vcp_id):
            raise FleetError(f"VCP '{vcp_id}' already exists", 409)
        if await self.store.charge_point_ids_exist([body.charge_point_id]):
            raise FleetError(f"charge point ID '{body.charge_point_id}' already exists", 409)
        profile_id = body.profile_id or fleet.profile_id
        if profile_id != fleet.profile_id:
            await self._check_profile(profile_id)
        await self.store.add_vcps([{
            "vcp_id": vcp_id,
            "charge_point_id": body.charge_point_id,
            "profile_id": profile_id,
            "ocpp_version": body.ocpp_version or fleet.ocpp_version or "1.6J",
            "group_id": body.group_id or fleet.group_id,
            "fleet_id": fleet_id,
            "worker_id": fleet.worker_id,
            "tags": body.tags or [],
            "meta": {**(body.meta or {}), "generated": False},
        }])
        await self.store.bump_fleet_config(fleet_id)
        return await self.store.get_vcp(vcp_id)

    async def delete(self, fleet_id: str, force: bool = False) -> None:
        fleet = await self.get(fleet_id)
        if fleet.status in ACTIVE_STATUSES:
            if not force:
                raise FleetError("stop the fleet before deleting it (or pass force=true)", 409)
            await self.stop(fleet_id)
        await self.store.delete_fleet(fleet_id)
        self.directory.remove(fleet.worker_id)

    async def get(self, fleet_id: str) -> Any:
        fleet = await self.store.get_fleet(fleet_id)
        if fleet is None:
            raise FleetError(f"fleet '{fleet_id}' not found", 404)
        return fleet

    # ------------------------------------------------------------------
    # Config preview / deploy / stop
    # ------------------------------------------------------------------

    async def preview_config(self, fleet_id: str) -> dict[str, Any]:
        fleet = await self.get(fleet_id)
        vcps = await self.store.fleet_vcps(fleet_id)
        launcher = get_launcher(fleet.launcher, self.settings)
        try:
            cfg = build_worker_config(fleet, vcps, self.settings, for_container=launcher.uses_container_paths())
        except FleetConfigError as exc:
            raise FleetError(str(exc), 422) from exc
        if cfg["csms"]["auth"].get("password"):
            cfg["csms"]["auth"]["password"] = "********"
        if cfg["redis"].get("password"):
            cfg["redis"]["password"] = "********"
        if cfg["control_api"].get("token"):
            cfg["control_api"]["token"] = "********"
        return cfg

    async def deploy(self, fleet_id: str) -> Any:
        fleet = await self.get(fleet_id)
        launcher = get_launcher(fleet.launcher, self.settings)
        if fleet.status in ACTIVE_STATUSES and await launcher.is_alive(fleet):
            raise FleetError("fleet is already running — use redeploy to apply changes", 409)

        ok, msg = await launcher.check()
        if not ok:
            raise FleetError(f"{fleet.launcher} launcher unavailable: {msg}", 503)

        vcps = await self.store.fleet_vcps(fleet_id)
        profiles = {p.profile_id: p for p in await self.store.list_profiles() if p.status == "active"}
        try:
            cfg_path = write_fleet_files(fleet, vcps, profiles, self.settings,
                                         for_container=launcher.uses_container_paths())
        except FleetConfigError as exc:
            raise FleetError(str(exc), 422) from exc

        await self.store.update_fleet(fleet_id, status="deploying", last_error="", config_path=str(cfg_path),
                                      runtime={}, deployed_at=None)
        try:
            result = await launcher.start(fleet, cfg_path)
        except (LauncherError, OSError) as exc:
            await self.store.update_fleet(fleet_id, status="failed", last_error=str(exc))
            raise FleetError(f"worker start failed: {exc}", 502) from exc

        await self.store._db.execute(  # noqa: SLF001
            update(VcpInstance).where(VcpInstance.fleet_id == fleet_id).values(worker_id=fleet.worker_id)
        )
        fleet = await self.store.update_fleet(
            fleet_id,
            runtime=result.runtime,
            worker_url=result.worker_url,
            deployed_at=_now(),
            stopped_at=None,
            deployed_config_version=fleet.config_version,
        )
        self.directory.upsert(fleet.worker_id, url=result.worker_url, source="fleet",
                              fleet_id=fleet_id, status="unknown")
        logger.info("Fleet %s deployed (%s) → %s", fleet_id, fleet.launcher, result.runtime)
        return fleet

    async def stop(self, fleet_id: str) -> Any:
        fleet = await self.get(fleet_id)
        launcher = get_launcher(fleet.launcher, self.settings)
        await self.store.update_fleet(fleet_id, status="stopping")
        try:
            await launcher.stop(fleet)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop of fleet %s raised: %s", fleet_id, exc)
        await self._mark_vcps_offline(fleet_id)
        self.directory.upsert(fleet.worker_id, status="offline")
        return await self.store.update_fleet(fleet_id, status="stopped", stopped_at=_now(), runtime={})

    async def redeploy(self, fleet_id: str) -> Any:
        fleet = await self.get(fleet_id)
        if fleet.status in ACTIVE_STATUSES:
            await self.stop(fleet_id)
        return await self.deploy(fleet_id)

    async def logs(self, fleet_id: str, tail: int = 200) -> str:
        fleet = await self.get(fleet_id)
        return await get_launcher(fleet.launcher, self.settings).logs(fleet, tail)

    async def _mark_vcps_offline(self, fleet_id: str) -> None:
        await self.store._db.execute(  # noqa: SLF001
            update(VcpInstance).where(VcpInstance.fleet_id == fleet_id)
            .values(connection_state="disconnected", actual_state="offline", updated_at=_now())
        )
        await self.store._db.commit()  # noqa: SLF001

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    async def monitor_once(self) -> list[str]:
        """Mark fleets whose worker died as failed.  Returns affected fleet IDs."""
        failed = []
        now = _now()
        for fleet in await self.store.list_fleets():
            if fleet.status not in ("deploying", "running"):
                continue
            # A deploy in progress has no runtime handle yet (or just got one):
            # give the launcher a short grace period before judging liveness.
            if not fleet.runtime:
                continue
            if fleet.status == "deploying" and fleet.deployed_at is not None:
                deployed = fleet.deployed_at if fleet.deployed_at.tzinfo else fleet.deployed_at.replace(tzinfo=timezone.utc)
                if (now - deployed).total_seconds() < MONITOR_GRACE_S:
                    continue
            launcher = get_launcher(fleet.launcher, self.settings)
            try:
                alive = await launcher.is_alive(fleet)
            except Exception:  # noqa: BLE001
                continue
            if alive:
                continue
            code = getattr(launcher, "exit_code", lambda f: None)(fleet)
            tail = (await launcher.logs(fleet, 8)).strip().splitlines()
            reason = f"worker exited{f' (code {code})' if code is not None else ''}"
            if tail:
                reason += ": " + " | ".join(tail[-3:])
            await self.store.update_fleet(fleet.fleet_id, status="failed", last_error=reason[:2000],
                                          stopped_at=_now())
            await self._mark_vcps_offline(fleet.fleet_id)
            self.directory.upsert(fleet.worker_id, status="offline")
            failed.append(fleet.fleet_id)
            logger.warning("Fleet %s marked failed: %s", fleet.fleet_id, reason)
        return failed


def fleet_files_dir(settings: Settings, fleet_id: str):
    return fleet_dir(settings, fleet_id)
