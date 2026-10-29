"""WebSocket event streaming endpoint.

Clients connect to /api/v1/events/ws and receive real-time JSON frames as
the EventSubscriber reads them off the worker Redis event streams.

Frame shape
-----------
{
  "event": "<event_type>",   # e.g. "state_change", "meter_value", "ocpp_trace"
  "vcpId": "<vcp_id>",
  "workerId": "<worker_id>",
  "payload": { ... },        # event-specific data
  "ts": "<ISO-8601>"
}

Filtering (optional query params)
----------------------------------
  vcp_id   — forward only frames for this VCP
  group_id — forward only frames whose VCP belongs to this group (uses DB)
  events   — comma-separated list of event types to forward
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from vcp_control_plane.config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level registry of active WebSocket connections.
# The EventSubscriber (started in app lifespan) calls broadcast_event() to push
# frames to all subscribed clients.
_connections: set["_FilteredConnection"] = set()


class _FilteredConnection:
    """Wraps a WebSocket with optional vcp_id / group_id / event-type filters."""

    def __init__(
        self,
        ws: WebSocket,
        vcp_id: str | None,
        group_vcps: set[str] | None,
        event_types: set[str] | None,
    ):
        self.ws = ws
        self.vcp_id = vcp_id
        self.group_vcps = group_vcps  # pre-resolved set of VCP IDs for the group
        self.event_types = event_types

    def accepts(self, frame: dict) -> bool:
        evt_vcp = frame.get("vcpId", "")
        if self.vcp_id and evt_vcp != self.vcp_id:
            return False
        if self.group_vcps is not None and evt_vcp not in self.group_vcps:
            return False
        if self.event_types and frame.get("event") not in self.event_types:
            return False
        return True

    async def send(self, frame: dict) -> None:
        try:
            await self.ws.send_json(frame)
        except Exception:
            pass  # disconnected — will be removed by the read loop


async def broadcast_event(frame: dict) -> None:
    """Called by EventSubscriber to push a frame to all matching WebSocket clients."""
    dead: list[_FilteredConnection] = []
    for conn in list(_connections):
        if conn.accepts(frame):
            try:
                await conn.send(frame)
            except Exception:
                dead.append(conn)
    for d in dead:
        _connections.discard(d)


@router.websocket("/events/ws")
async def events_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    vcp_id: str | None = Query(default=None, alias="vcpId"),
    group_id: str | None = Query(default=None, alias="groupId"),
    events: str | None = Query(default=None),
) -> None:
    """WebSocket endpoint for real-time VCP event streaming."""
    settings = get_settings()

    # Token auth via query-param (WS clients can't set headers easily)
    if settings.require_auth and settings.api_token:
        if token != settings.api_token:
            await websocket.close(code=4001, reason="Unauthorized")
            return

    await websocket.accept()

    # Resolve group → VCP ID set once at connect time
    group_vcps: set[str] | None = None
    if group_id:
        try:
            from vcp_control_plane.db.base import get_db_session
            from vcp_control_plane.registry.store import VcpRegistryStore
            async for session in get_db_session():
                store = VcpRegistryStore(session)
                rows, _ = await store.list_vcps(group_id=group_id, page=1, page_size=10000)
                group_vcps = {r.vcp_id for r in rows}
                break
        except Exception as exc:
            logger.warning("Could not resolve group '%s' for WS filter: %s", group_id, exc)
            group_vcps = set()  # nothing passes the filter → safe default

    event_types: set[str] | None = (
        {e.strip() for e in events.split(",") if e.strip()} if events else None
    )

    conn = _FilteredConnection(
        ws=websocket,
        vcp_id=vcp_id,
        group_vcps=group_vcps,
        event_types=event_types,
    )
    _connections.add(conn)
    logger.info(
        "WS client connected (vcp_id=%s, group_id=%s, events=%s). Total: %d",
        vcp_id,
        group_id,
        events,
        len(_connections),
    )

    try:
        # Keep connection alive; discard any client messages (ping/pong handled by FastAPI)
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a heartbeat so the client knows we're alive
                try:
                    await websocket.send_json({"event": "heartbeat"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        _connections.discard(conn)
        logger.info(
            "WS client disconnected. Remaining: %d", len(_connections)
        )
