"""VCP Registry Store — SQLAlchemy async CRUD layer.

All database interactions for the control plane go through this class.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from vcp_control_plane.db.models import (
    ChargerProfile,
    CommandAudit,
    TestExecution,
    VcpFleet,
    VcpInstance,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VcpRegistryStore:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    # ------------------------------------------------------------------
    # VCP Instance
    # ------------------------------------------------------------------

    async def create_vcp(self, body) -> VcpInstance:
        """Create a new VCP registry entry."""
        row = VcpInstance(
            vcp_id=body.vcp_id or str(uuid.uuid4()),
            charge_point_id=body.charge_point_id,
            profile_id=body.profile_id or "",
            profile_version=getattr(body, "profile_version", "latest") or "latest",
            ocpp_version=body.ocpp_version or "1.6J",
            worker_id=body.worker_id,
            group_id=body.group_id,
            fleet_id=getattr(body, "fleet_id", None),
            desired_state=getattr(body, "desired_state", None) or "idle",
            actual_state="unknown",
            connection_state="disconnected",
            connector_state={},
            tags=body.tags or [],
            meta=body.meta or {},
            created_at=_now(),
            updated_at=_now(),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_vcp(self, vcp_id: str) -> VcpInstance | None:
        result = await self._db.execute(
            select(VcpInstance).where(VcpInstance.vcp_id == vcp_id)
        )
        return result.scalar_one_or_none()

    async def list_vcps(
        self,
        page: int = 1,
        page_size: int = 50,
        group_id: str | None = None,
        worker_id: str | None = None,
        actual_state: str | None = None,
        profile_id: str | None = None,
        fleet_id: str | None = None,
    ) -> tuple[list[VcpInstance], int]:
        q = select(VcpInstance)
        count_q = select(func.count()).select_from(VcpInstance)

        if fleet_id:
            q = q.where(VcpInstance.fleet_id == fleet_id)
            count_q = count_q.where(VcpInstance.fleet_id == fleet_id)
        if group_id:
            q = q.where(VcpInstance.group_id == group_id)
            count_q = count_q.where(VcpInstance.group_id == group_id)
        if worker_id:
            q = q.where(VcpInstance.worker_id == worker_id)
            count_q = count_q.where(VcpInstance.worker_id == worker_id)
        if actual_state:
            q = q.where(VcpInstance.actual_state == actual_state)
            count_q = count_q.where(VcpInstance.actual_state == actual_state)
        if profile_id:
            q = q.where(VcpInstance.profile_id == profile_id)
            count_q = count_q.where(VcpInstance.profile_id == profile_id)

        total_result = await self._db.execute(count_q)
        total = total_result.scalar_one()

        q = q.order_by(VcpInstance.created_at.desc())
        q = q.offset((page - 1) * page_size).limit(page_size)
        rows_result = await self._db.execute(q)
        rows = list(rows_result.scalars().all())
        return rows, total

    async def patch_vcp(self, vcp_id: str, body) -> VcpInstance | None:
        row = await self.get_vcp(vcp_id)
        if row is None:
            return None
        updates: dict[str, Any] = {"updated_at": _now()}
        if body.desired_state is not None:
            updates["desired_state"] = body.desired_state
        if body.worker_id is not None:
            updates["worker_id"] = body.worker_id
        if body.group_id is not None:
            updates["group_id"] = body.group_id
        if body.tags is not None:
            updates["tags"] = body.tags
        if body.meta is not None:
            updates["meta"] = body.meta
        if body.profile_id is not None:
            updates["profile_id"] = body.profile_id

        await self._db.execute(
            update(VcpInstance).where(VcpInstance.vcp_id == vcp_id).values(**updates)
        )
        await self._db.commit()
        return await self.get_vcp(vcp_id)

    async def delete_vcp(self, vcp_id: str) -> bool:
        result = await self._db.execute(
            delete(VcpInstance).where(VcpInstance.vcp_id == vcp_id)
        )
        await self._db.commit()
        return result.rowcount > 0

    async def update_vcp_state(
        self,
        vcp_id: str,
        actual_state: str | None = None,
        connection_state: str | None = None,
        connector_state: dict | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """Called by EventSubscriber to apply worker-reported state updates.

        Returns False when the VCP is not in the registry (caller may auto-register).
        """
        updates: dict[str, Any] = {"updated_at": _now(), "last_seen": _now()}
        if actual_state is not None:
            updates["actual_state"] = actual_state
        if connection_state is not None:
            updates["connection_state"] = connection_state
        if connector_state is not None:
            updates["connector_state"] = connector_state
        if worker_id is not None:
            updates["worker_id"] = worker_id
        result = await self._db.execute(
            update(VcpInstance).where(VcpInstance.vcp_id == vcp_id).values(**updates)
        )
        await self._db.commit()
        return result.rowcount > 0

    async def upsert_vcp_from_worker(
        self,
        vcp_id: str,
        worker_id: str,
        charge_point_id: str | None = None,
        profile_id: str | None = None,
        group_id: str | None = None,
        ocpp_version: str | None = None,
        fleet_id: str | None = None,
    ) -> bool:
        """Register a VCP reported by a worker (worker_status / state_change).

        Existing rows only get their worker assignment refreshed; new rows are
        inserted.  Returns True when a row was created.
        """
        row = await self.get_vcp(vcp_id)
        if row is not None:
            if row.worker_id != worker_id:
                await self._db.execute(
                    update(VcpInstance).where(VcpInstance.vcp_id == vcp_id)
                    .values(worker_id=worker_id, updated_at=_now())
                )
                await self._db.commit()
            return False
        cp_id = charge_point_id or vcp_id
        clash = await self._db.execute(
            select(VcpInstance.vcp_id).where(VcpInstance.charge_point_id == cp_id)
        )
        if clash.scalar_one_or_none() is not None:
            return False  # charge_point_id already registered under another VCP ID
        now = _now()
        self._db.add(VcpInstance(
            vcp_id=vcp_id,
            charge_point_id=cp_id,
            profile_id=profile_id or "",
            ocpp_version=ocpp_version or "1.6J",
            worker_id=worker_id,
            group_id=group_id,
            fleet_id=fleet_id,
            desired_state="idle",
            actual_state="unknown",
            connection_state="disconnected",
            connector_state={},
            tags=["auto-registered"] if fleet_id is None else [],
            meta={},
            created_at=now,
            updated_at=now,
        ))
        await self._db.commit()
        return True

    async def aggregate_state_counts(self) -> dict:
        """Return total VCP count and state distribution dicts."""
        total_r = await self._db.execute(
            select(func.count()).select_from(VcpInstance)
        )
        total = total_r.scalar_one()

        conn_r = await self._db.execute(
            select(VcpInstance.connection_state, func.count())
            .group_by(VcpInstance.connection_state)
        )
        connection_states = {row[0]: row[1] for row in conn_r.all()}

        actual_r = await self._db.execute(
            select(VcpInstance.actual_state, func.count())
            .group_by(VcpInstance.actual_state)
        )
        actual_states = {row[0]: row[1] for row in actual_r.all()}

        return {
            "total": total,
            "connection_states": connection_states,
            "actual_states": actual_states,
        }

    # ------------------------------------------------------------------
    # Charger Profile
    # ------------------------------------------------------------------

    async def upsert_profile(self, body) -> ChargerProfile:
        existing = await self.get_profile(body.profile_id)
        if existing:
            updates: dict[str, Any] = {
                "version": body.version,
                "vendor": body.vendor,
                "model": body.model,
                "firmware": body.firmware,
                "ocpp_version": body.ocpp_version,
                "capability_definition": body.capability_definition or {},
                "behavior_definition": body.behavior_definition or {},
                "evse_definition": body.evse_definition or [],
                "tags": body.tags or [],
                "status": "active",
                "updated_at": _now(),
            }
            if hasattr(body, "source") and body.source:
                updates["source"] = body.source
            await self._db.execute(
                update(ChargerProfile)
                .where(ChargerProfile.profile_id == body.profile_id)
                .values(**updates)
            )
            await self._db.commit()
            return await self.get_profile(body.profile_id)  # type: ignore[return-value]

        row = ChargerProfile(
            profile_id=body.profile_id,
            version=body.version,
            vendor=body.vendor,
            model=body.model,
            firmware=body.firmware,
            ocpp_version=body.ocpp_version,
            capability_definition=body.capability_definition or {},
            behavior_definition=body.behavior_definition or {},
            evse_definition=body.evse_definition or [],
            source=getattr(body, "source", None) or "",
            tags=body.tags or [],
            status="active",
            validated=False,
            created_at=_now(),
            updated_at=_now(),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_profile(self, profile_id: str) -> ChargerProfile | None:
        result = await self._db.execute(
            select(ChargerProfile).where(ChargerProfile.profile_id == profile_id)
        )
        return result.scalar_one_or_none()

    async def list_profiles(self) -> list[ChargerProfile]:
        result = await self._db.execute(
            select(ChargerProfile).order_by(ChargerProfile.created_at.desc())
        )
        return list(result.scalars().all())

    async def deprecate_profile(self, profile_id: str) -> bool:
        result = await self._db.execute(
            update(ChargerProfile)
            .where(ChargerProfile.profile_id == profile_id)
            .values(status="deprecated", updated_at=_now())
        )
        await self._db.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Test Execution
    # ------------------------------------------------------------------

    async def create_execution(
        self,
        execution_id: str,
        scenario_id: str,
        scenario_name: str,
        target_scope: str,
        target_count: int,
        initiated_by: str | None = None,
        target_vcp_id: str | None = None,
        target_group_id: str | None = None,
    ) -> TestExecution:
        now = _now()
        row = TestExecution(
            execution_id=execution_id,
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            target_scope=target_scope,
            target_vcp_id=target_vcp_id,
            target_group_id=target_group_id,
            target_count=target_count,
            status="running",
            start_time=now,
            initiated_by=initiated_by,
            created_at=now,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_execution(self, execution_id: str) -> TestExecution | None:
        result = await self._db.execute(
            select(TestExecution).where(TestExecution.execution_id == execution_id)
        )
        return result.scalar_one_or_none()

    async def list_executions(self, limit: int = 100) -> list[TestExecution]:
        result = await self._db.execute(
            select(TestExecution)
            .order_by(TestExecution.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def cancel_execution(self, execution_id: str) -> bool:
        result = await self._db.execute(
            update(TestExecution)
            .where(
                TestExecution.execution_id == execution_id,
                TestExecution.status == "running",
            )
            .values(status="cancelled", end_time=_now())
        )
        await self._db.commit()
        return result.rowcount > 0

    async def finish_execution(
        self,
        execution_id: str,
        status: str,
        result: str | None = None,
        report: dict | None = None,
    ) -> None:
        await self._db.execute(
            update(TestExecution)
            .where(TestExecution.execution_id == execution_id)
            .values(
                status=status,
                result=result,
                report=report or {},
                end_time=_now(),
            )
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Command Audit
    # ------------------------------------------------------------------

    async def record_command(
        self,
        command_id: str,
        command_type: str,
        vcp_id: str | None = None,
        payload: dict | None = None,
        issued_by: str | None = None,
        group_id: str | None = None,
    ) -> CommandAudit:
        row = CommandAudit(
            command_id=command_id,
            command_type=command_type,
            vcp_id=vcp_id,
            group_id=group_id,
            payload=payload or {},
            issued_by=issued_by,
            status="dispatched",
            issued_at=_now(),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def list_commands(
        self,
        vcp_id: str | None = None,
        limit: int = 50,
    ) -> list[CommandAudit]:
        q = select(CommandAudit).order_by(CommandAudit.issued_at.desc()).limit(limit)
        if vcp_id:
            q = q.where(CommandAudit.vcp_id == vcp_id)
        result = await self._db.execute(q)
        return list(result.scalars().all())

    async def acknowledge_command(
        self,
        command_id: str,
        status: str = "acknowledged",
        result: dict | None = None,
        vcp_id: str | None = None,
    ) -> None:
        """Apply a worker command_result.

        Single-VCP commands take the reported status.  Broadcast commands (one
        audit row, many results) collect per-VCP outcomes under result["vcps"]
        and are "acknowledged" once any VCP succeeded.
        """
        row = (await self._db.execute(
            select(CommandAudit).where(CommandAudit.command_id == command_id)
        )).scalar_one_or_none()
        if row is None:
            return
        if row.vcp_id:
            new_status, new_result = status, (result or {})
        else:
            merged = dict(row.result or {})
            vcps = dict(merged.get("vcps") or {})
            vcps[vcp_id or "?"] = {"ok": status == "acknowledged", **({"error": (result or {}).get("error")}
                                                                       if status != "acknowledged" else {})}
            merged["vcps"] = vcps
            ok_count = sum(1 for v in vcps.values() if v.get("ok"))
            merged["ok_count"], merged["total"] = ok_count, len(vcps)
            new_status = "acknowledged" if ok_count else "failed"
            new_result = merged
        await self._db.execute(
            update(CommandAudit)
            .where(CommandAudit.command_id == command_id)
            .values(status=new_status, acknowledged_at=_now(), result=new_result)
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Scenario progress (scenario_status events)
    # ------------------------------------------------------------------

    async def record_scenario_status(
        self,
        execution_id: str,
        vcp_id: str,
        status: str,
        message: str = "",
    ) -> TestExecution | None:
        """Track per-VCP scenario progress and close the execution when all
        targeted VCPs reached a terminal state."""
        row = await self.get_execution(execution_id)
        if row is None:
            return None
        report = dict(row.report or {})
        vcps = dict(report.get("vcps") or {})
        prev = vcps.get(vcp_id, {})
        if prev.get("status") in ("completed", "failed", "cancelled") and status in ("running", "step"):
            return row  # late progress after a terminal state
        vcps[vcp_id] = {"status": status, "message": message, "ts": _now().isoformat()}
        report["vcps"] = vcps
        counts: dict[str, int] = {}
        for v in vcps.values():
            counts[v["status"]] = counts.get(v["status"], 0) + 1
        report["counts"] = counts

        values: dict[str, Any] = {"report": report}
        terminal = sum(counts.get(k, 0) for k in ("completed", "failed", "cancelled"))
        if row.status in ("pending", "running"):
            values["status"] = "running"
            if row.target_count and terminal >= row.target_count:
                passed = counts.get("completed", 0)
                values["status"] = "completed" if passed else "failed"
                values["result"] = (
                    "passed" if passed == row.target_count else "failed" if passed == 0 else "partial"
                )
                values["end_time"] = _now()
        await self._db.execute(
            update(TestExecution).where(TestExecution.execution_id == execution_id).values(**values)
        )
        await self._db.commit()
        return await self.get_execution(execution_id)

    # ------------------------------------------------------------------
    # VCP Fleet
    # ------------------------------------------------------------------

    async def create_fleet(self, **fields: Any) -> VcpFleet:
        now = _now()
        row = VcpFleet(created_at=now, updated_at=now, **fields)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def get_fleet(self, fleet_id: str) -> VcpFleet | None:
        return (await self._db.execute(
            select(VcpFleet).where(VcpFleet.fleet_id == fleet_id)
        )).scalar_one_or_none()

    async def get_fleet_by_worker(self, worker_id: str) -> VcpFleet | None:
        return (await self._db.execute(
            select(VcpFleet).where(VcpFleet.worker_id == worker_id)
        )).scalar_one_or_none()

    async def list_fleets(self) -> list[VcpFleet]:
        return list((await self._db.execute(
            select(VcpFleet).order_by(VcpFleet.created_at.desc())
        )).scalars().all())

    async def update_fleet(self, fleet_id: str, **values: Any) -> VcpFleet | None:
        values["updated_at"] = _now()
        await self._db.execute(update(VcpFleet).where(VcpFleet.fleet_id == fleet_id).values(**values))
        await self._db.commit()
        row = await self.get_fleet(fleet_id)
        if row is not None:
            await self._db.refresh(row)
        return row

    async def bump_fleet_config(self, fleet_id: str) -> None:
        await self._db.execute(
            update(VcpFleet).where(VcpFleet.fleet_id == fleet_id)
            .values(config_version=VcpFleet.config_version + 1, updated_at=_now())
        )
        await self._db.commit()

    async def delete_fleet(self, fleet_id: str) -> bool:
        await self._db.execute(delete(VcpInstance).where(VcpInstance.fleet_id == fleet_id))
        result = await self._db.execute(delete(VcpFleet).where(VcpFleet.fleet_id == fleet_id))
        await self._db.commit()
        return result.rowcount > 0

    async def fleet_vcps(self, fleet_id: str) -> list[VcpInstance]:
        return list((await self._db.execute(
            select(VcpInstance).where(VcpInstance.fleet_id == fleet_id).order_by(VcpInstance.vcp_id)
        )).scalars().all())

    async def fleet_vcp_counts(self) -> dict[str, dict[str, int]]:
        """fleet_id → {total, connected}."""
        rows = (await self._db.execute(
            select(VcpInstance.fleet_id, VcpInstance.connection_state, func.count())
            .where(VcpInstance.fleet_id.is_not(None))
            .group_by(VcpInstance.fleet_id, VcpInstance.connection_state)
        )).all()
        out: dict[str, dict[str, int]] = {}
        for fid, conn, n in rows:
            d = out.setdefault(fid, {"total": 0, "connected": 0})
            d["total"] += n
            if conn == "connected":
                d["connected"] += n
        return out

    async def used_ports(self) -> tuple[set[int], set[int]]:
        rows = (await self._db.execute(select(VcpFleet.api_port, VcpFleet.metrics_port))).all()
        return {r[0] for r in rows if r[0]}, {r[1] for r in rows if r[1]}

    async def vcp_ids_exist(self, vcp_ids: list[str]) -> set[str]:
        if not vcp_ids:
            return set()
        return set((await self._db.execute(
            select(VcpInstance.vcp_id).where(VcpInstance.vcp_id.in_(vcp_ids))
        )).scalars().all())

    async def charge_point_ids_exist(self, cp_ids: list[str]) -> set[str]:
        if not cp_ids:
            return set()
        return set((await self._db.execute(
            select(VcpInstance.charge_point_id).where(VcpInstance.charge_point_id.in_(cp_ids))
        )).scalars().all())

    async def add_vcps(self, rows: list[dict[str, Any]]) -> None:
        now = _now()
        for r in rows:
            self._db.add(VcpInstance(
                desired_state="idle", actual_state="unknown", connection_state="disconnected",
                connector_state={}, tags=r.pop("tags", []), meta=r.pop("meta", {}),
                created_at=now, updated_at=now, **r,
            ))
        await self._db.commit()
