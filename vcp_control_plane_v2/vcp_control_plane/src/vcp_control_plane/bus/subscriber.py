"""Event Subscriber — consumes worker events from Redis Streams (contract v2).

Reads from ``{event_stream_prefix}:{worker_id}`` for every worker in the
WorkerDirectory (static config, deployed fleets, Redis discovery).  The set
of streams is refreshed every ``event_stream_refresh_s`` so fleets deployed
from the UI are picked up without restarting the control plane.

Per event type:
  worker_status    update the directory + fleet runtime status; auto-register
                   the VCPs the worker announces
  state_change     persist actual/connection/connector state (auto-registers
                   an unknown VCP first)
  command_result   mark the command_audit row acknowledged / failed
  scenario_status  per-VCP scenario progress → test_execution status/result
  meter_value, ocpp_trace
                   forwarded to WebSocket clients only (no DB write)

Every event is forwarded to WebSocket clients (Live Events page).

Worker event message fields (all strings): event, vcp_id, worker_id,
payload (JSON), ts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis

from vcp_control_plane.bus import contract
from vcp_control_plane.config.settings import Settings
from vcp_control_plane.workers.directory import WorkerDirectory, get_worker_directory

logger = logging.getLogger(__name__)

_COUNT = 100
_RETRY_DELAY_S = 5.0

BroadcastFn = Callable[[dict], Awaitable[None]]


class EventSubscriber:
    """Async background task that tails worker event streams."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        settings: Settings,
        session_factory: Any = None,
        directory: WorkerDirectory | None = None,
        broadcast: BroadcastFn | None = None,
    ) -> None:
        self._redis = redis_client
        self._settings = settings
        self._session_factory = session_factory
        self._directory = directory or get_worker_directory()
        self._broadcast = broadcast
        self._running = False
        self._groups_ready: set[str] = set()
        self.processed = 0

    # ------------------------------------------------------------------
    # Wiring helpers
    # ------------------------------------------------------------------

    def _sessions(self):
        if self._session_factory is None:
            from vcp_control_plane.db.base import get_session_factory
            self._session_factory = get_session_factory()
        return self._session_factory

    async def _forward(self, frame: dict) -> None:
        if self._broadcast is not None:
            await self._broadcast(frame)
            return
        from vcp_control_plane.api.routers.events import broadcast_event
        await broadcast_event(frame)

    # ------------------------------------------------------------------
    # Stream management
    # ------------------------------------------------------------------

    async def refresh_streams(self) -> list[str]:
        """Refresh the worker directory and return the event streams to read."""
        fleets = None
        try:
            from vcp_control_plane.registry.store import VcpRegistryStore
            async with self._sessions()() as session:
                fleets = await VcpRegistryStore(session).list_fleets()
        except Exception as exc:  # noqa: BLE001
            logger.debug("fleet lookup for stream refresh failed: %s", exc)
        await self._directory.refresh(self._settings, self._redis, fleets)
        streams = self._directory.event_streams(self._settings)
        await self._ensure_consumer_groups(streams)
        return streams

    async def _ensure_consumer_groups(self, streams: list[str]) -> None:
        group = self._settings.consumer_group
        for stream in streams:
            if stream in self._groups_ready:
                continue
            try:
                # "0": deliver events already in the stream (e.g. the worker's
                # "online" announcement published before we subscribed).  The
                # group persists, so this backlog is read only once.
                await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
                logger.info("Created consumer group '%s' on stream '%s'", group, stream)
            except aioredis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    logger.warning("xgroup_create error on %s: %s", stream, exc)
                    continue
            self._groups_ready.add(stream)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        group = self._settings.consumer_group
        consumer = self._settings.consumer_name
        loop = asyncio.get_running_loop()
        next_refresh = 0.0
        streams: list[str] = []
        logger.info("EventSubscriber starting (group=%s consumer=%s)", group, consumer)

        while self._running:
            try:
                if loop.time() >= next_refresh:
                    new_streams = await self.refresh_streams()
                    if new_streams != streams:
                        logger.info("EventSubscriber streams: %s", new_streams)
                    streams = new_streams
                    next_refresh = loop.time() + self._settings.event_stream_refresh_s

                results = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={s: ">" for s in streams},
                    count=_COUNT,
                    block=self._settings.stream_block_ms or 1000,
                )
            except asyncio.CancelledError:
                logger.info("EventSubscriber cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("EventSubscriber Redis error: %s — retrying in %ss", exc, _RETRY_DELAY_S)
                self._groups_ready.clear()
                await asyncio.sleep(_RETRY_DELAY_S)
                continue

            for stream_name, messages in results or []:
                for msg_id, fields in messages:
                    await self.handle_message(stream_name, msg_id, fields, group)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_message(self, stream: str, msg_id: str, fields: dict, group: str | None = None) -> None:
        group = group or self._settings.consumer_group
        try:
            event_type = fields.get("event", "unknown")
            vcp_id = fields.get("vcp_id", "")
            worker_id = fields.get("worker_id", "")
            ts = fields.get("ts", "") or datetime.now(timezone.utc).isoformat()
            try:
                payload = json.loads(fields.get("payload", "{}"))
            except json.JSONDecodeError:
                payload = {}

            if worker_id:
                self._directory.touch(worker_id)

            if event_type == contract.EVT_WORKER_STATUS and worker_id:
                await self._on_worker_status(worker_id, payload)
            elif event_type == contract.EVT_STATE_CHANGE and vcp_id:
                await self._on_state_change(vcp_id, worker_id, payload)
            elif event_type == contract.EVT_COMMAND_RESULT:
                await self._on_command_result(vcp_id, payload)
            elif event_type == contract.EVT_SCENARIO_STATUS and payload.get("execution_id"):
                await self._on_scenario_status(vcp_id, payload)

            await self._forward({
                "event": event_type,
                "vcpId": vcp_id,
                "workerId": worker_id,
                "payload": payload,
                "ts": ts,
            })
            self.processed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Error handling msg %s on %s: %s", msg_id, stream, exc, exc_info=True)
        finally:
            # Ack even on persistent parse errors to avoid redelivery loops
            try:
                await self._redis.xack(stream, group, msg_id)
            except Exception:  # noqa: BLE001
                pass

    async def _on_worker_status(self, worker_id: str, payload: dict[str, Any]) -> None:
        from vcp_control_plane.registry.store import VcpRegistryStore

        self._directory.on_worker_status(worker_id, payload)
        status = payload.get("status")
        async with self._sessions()() as session:
            store = VcpRegistryStore(session)
            fleet = await store.get_fleet_by_worker(worker_id)
            if fleet is not None:
                now = datetime.now(timezone.utc)
                if status == "offline":
                    # deploying / stopping are driven by the control plane itself
                    # (a late "offline" from the previous worker instance during a
                    # redeploy must not override them)
                    if fleet.status == "running":
                        await store.update_fleet(fleet.fleet_id, status="stopped", stopped_at=now, last_seen=now)
                else:
                    values: dict[str, Any] = {"last_seen": now}
                    if fleet.status in ("deploying", "failed"):
                        values.update(status="running", last_error="")
                    await store.update_fleet(fleet.fleet_id, **values)
            for v in payload.get("vcps") or []:
                await store.upsert_vcp_from_worker(
                    vcp_id=v.get("vcp_id", ""),
                    worker_id=worker_id,
                    charge_point_id=v.get("charge_point_id"),
                    profile_id=v.get("profile_id"),
                    group_id=v.get("group_id"),
                    ocpp_version=v.get("ocpp_version"),
                    fleet_id=fleet.fleet_id if fleet is not None else None,
                )

    async def _on_state_change(self, vcp_id: str, worker_id: str, payload: dict[str, Any]) -> None:
        from vcp_control_plane.registry.store import VcpRegistryStore

        async with self._sessions()() as session:
            store = VcpRegistryStore(session)
            kwargs = dict(
                vcp_id=vcp_id,
                actual_state=payload.get("actual_state"),
                connection_state=payload.get("connection_state"),
                connector_state=payload.get("connector_state"),
                worker_id=worker_id or None,
            )
            if not await store.update_vcp_state(**kwargs):
                fleet = await store.get_fleet_by_worker(worker_id) if worker_id else None
                await store.upsert_vcp_from_worker(
                    vcp_id=vcp_id,
                    worker_id=worker_id,
                    charge_point_id=payload.get("charge_point_id"),
                    profile_id=payload.get("profile_id"),
                    group_id=payload.get("group_id"),
                    ocpp_version=payload.get("ocpp_version"),
                    fleet_id=fleet.fleet_id if fleet is not None else None,
                )
                await store.update_vcp_state(**kwargs)

    async def _on_command_result(self, vcp_id: str, payload: dict[str, Any]) -> None:
        from vcp_control_plane.registry.store import VcpRegistryStore

        command_id = payload.get("command_id")
        if not command_id:
            return
        async with self._sessions()() as session:
            await VcpRegistryStore(session).acknowledge_command(
                command_id,
                status="acknowledged" if payload.get("ok") else "failed",
                result={k: payload.get(k) for k in ("ok", "error", "result")},
                vcp_id=vcp_id or None,
            )

    async def _on_scenario_status(self, vcp_id: str, payload: dict[str, Any]) -> None:
        from vcp_control_plane.registry.store import VcpRegistryStore

        async with self._sessions()() as session:
            await VcpRegistryStore(session).record_scenario_status(
                execution_id=payload["execution_id"],
                vcp_id=vcp_id,
                status=str(payload.get("status", "")),
                message=str(payload.get("message", "")),
            )
