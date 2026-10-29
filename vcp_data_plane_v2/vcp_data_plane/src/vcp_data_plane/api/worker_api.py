"""Worker local HTTP API.

Provides a FastAPI application exposing health, status and control endpoints
for this worker process.  Used by:
  - Control plane gateway to fan out VCP-specific commands
  - Kubernetes/Docker health checks
  - Monitoring dashboards

Endpoints:
  GET  /health              — liveness probe
  GET  /ready               — readiness probe (Redis connected, VCPs running)
  GET  /status              — worker status summary
  GET  /fleet/summary       — shard summary in the control-plane shape
                              (camelCase; polled by GET /api/v1/fleet/summary)
  GET  /vcps                — list all VCPs with current state
  GET  /vcps/{vcp_id}       — single VCP state detail
  POST /vcps/{vcp_id}/command  — send a command to one VCP
  POST /fleet/command       — send a command to a group or all VCPs
  GET  /metrics             — (handled by prometheus-client, separate port)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from vcp_data_plane.config.schema import CommandType, WorkerCommand
from vcp_data_plane.state.machine import VcpState
from vcp_data_plane.state.registry import VcpRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class VcpCommandRequest(BaseModel):
    command_type: CommandType
    payload: dict[str, Any] = {}
    issued_by: str = "worker-api"


class FleetCommandRequest(BaseModel):
    command_type: CommandType
    group_id: str | None = None
    payload: dict[str, Any] = {}
    issued_by: str = "worker-api"


# ---------------------------------------------------------------------------
# API builder
# ---------------------------------------------------------------------------

def build_worker_api(
    worker_id: str,
    registry: VcpRegistry,
    command_dispatcher: Any,    # callable(vcp_id, command) → dict
    control_token: str = "",
    require_auth: bool = True,
    group_of: Any = None,       # callable(vcp_id) → group_id | None
) -> FastAPI:
    """Build and return the worker FastAPI application."""

    app = FastAPI(
        title=f"VCP Worker API — {worker_id}",
        version="1.0.0",
        docs_url="/docs",
    )

    def _check_auth(authorization: str | None = Header(default=None)) -> None:
        if not require_auth:
            return
        if not control_token:
            return
        expected = f"Bearer {control_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "worker_id": worker_id}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        vcp_count = registry.count()
        return {
            "ready": vcp_count > 0,
            "worker_id": worker_id,
            "vcp_count": vcp_count,
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @app.get("/status")
    async def status(_: None = Depends(_check_auth)) -> dict[str, Any]:
        state_counts = registry.summary()
        return {
            "worker_id": worker_id,
            "vcp_total": registry.count(),
            "state_distribution": state_counts,
        }

    @app.get("/fleet/summary")
    async def fleet_summary(_: None = Depends(_check_auth)) -> dict[str, Any]:
        """Shard summary consumed by the control plane (contract v2)."""
        from vcp_data_plane.contract import CONTRACT_VERSION, normalise_connection_state

        connector_counts: dict[str, int] = {}
        connection_counts: dict[str, int] = {}
        for m in registry.all().values():
            st = m.primary.state.value
            connector_counts[st] = connector_counts.get(st, 0) + 1
            cs = normalise_connection_state(m.connection_state.value)
            connection_counts[cs] = connection_counts.get(cs, 0) + 1
        return {
            "workerId": worker_id,
            "contractVersion": CONTRACT_VERSION,
            "vcpCount": registry.count(),
            "connectorStateCounts": connector_counts,
            "connectionStateCounts": connection_counts,
        }

    # ------------------------------------------------------------------
    # VCP listing and detail
    # ------------------------------------------------------------------

    @app.get("/vcps")
    async def list_vcps(_: None = Depends(_check_auth)) -> dict[str, Any]:
        machines = registry.all()
        return {
            "worker_id": worker_id,
            "count": len(machines),
            "vcps": [m.snapshot() for m in machines.values()],
        }

    @app.get("/vcps/{vcp_id}")
    async def get_vcp(vcp_id: str, _: None = Depends(_check_auth)) -> dict[str, Any]:
        machine = registry.get(vcp_id)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"VCP {vcp_id!r} not found on this worker")
        return machine.snapshot()

    # ------------------------------------------------------------------
    # Per-VCP command
    # ------------------------------------------------------------------

    @app.post("/vcps/{vcp_id}/command")
    async def vcp_command(
        vcp_id: str,
        req: VcpCommandRequest,
        _: None = Depends(_check_auth),
    ) -> dict[str, Any]:
        machine = registry.get(vcp_id)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"VCP {vcp_id!r} not found on this worker")

        import uuid
        import datetime
        command = WorkerCommand(
            command_id=str(uuid.uuid4()),
            command_type=req.command_type,
            vcp_id=vcp_id,
            payload=req.payload,
            issued_by=req.issued_by,
            issued_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

        try:
            result = await command_dispatcher(vcp_id, command)
            return {"ok": True, "vcp_id": vcp_id, "command_type": req.command_type, "result": result}
        except Exception as exc:  # noqa: BLE001
            logger.error("Command dispatch error for %s: %s", vcp_id, exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Fleet / group command
    # ------------------------------------------------------------------

    @app.post("/fleet/command")
    async def fleet_command(
        req: FleetCommandRequest,
        _: None = Depends(_check_auth),
    ) -> dict[str, Any]:
        import uuid
        import datetime

        machines = registry.all()
        results = []

        for vcp_id, machine in machines.items():
            if req.group_id and group_of is not None and group_of(vcp_id) != req.group_id:
                continue
            command = WorkerCommand(
                command_id=str(uuid.uuid4()),
                command_type=req.command_type,
                vcp_id=vcp_id,
                group_id=req.group_id,
                payload=req.payload,
                issued_by=req.issued_by,
                issued_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            try:
                result = await command_dispatcher(vcp_id, command)
                results.append({"vcp_id": vcp_id, "ok": True, "result": result})
            except Exception as exc:  # noqa: BLE001
                results.append({"vcp_id": vcp_id, "ok": False, "error": str(exc)})

        success_count = sum(1 for r in results if r["ok"])
        return {
            "total": len(results),
            "success": success_count,
            "failed": len(results) - success_count,
            "results": results,
        }

    # ------------------------------------------------------------------
    # Scenario control
    # ------------------------------------------------------------------

    @app.post("/vcps/{vcp_id}/scenario")
    async def run_scenario(
        vcp_id: str,
        body: dict[str, Any],
        _: None = Depends(_check_auth),
    ) -> dict[str, Any]:
        machine = registry.get(vcp_id)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"VCP {vcp_id!r} not found")

        scenario_name = body.get("scenario")
        params = body.get("params", {})
        if not scenario_name:
            raise HTTPException(status_code=400, detail="'scenario' field required")

        # Dispatch via command
        import uuid
        import datetime
        command = WorkerCommand(
            command_id=str(uuid.uuid4()),
            command_type=CommandType.RUN_SCENARIO,
            vcp_id=vcp_id,
            payload={"scenario": scenario_name, "params": params},
            issued_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        result = await command_dispatcher(vcp_id, command)
        return {"ok": True, "vcp_id": vcp_id, "scenario": scenario_name, "result": result}

    return app
