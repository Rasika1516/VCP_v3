"""Redis Streams Command Listener.

Consumes WorkerCommand messages from two Redis Streams:
  1. vcp:commands:worker:{worker_id}   — per-worker commands (VCP-specific)
  2. vcp:commands:broadcast            — fleet/group broadcast commands

Architecture rationale for Redis Streams (vs Kafka):
  - At 10k+ VCPs across ~20 workers, per-worker streams give each worker its
    own consumer group; commands are delivered exactly once via XACK.
  - Commands are typically low volume (<1000/s peak); Redis Streams trivially
    handles this.  Kafka's overhead (broker cluster, topic partitions, consumer
    rebalancing) is not justified at this scale.
  - Redis also serves VCP state caching, making it a single dependency rather
    than two.
  - Pending-Entry List (PEL) and XAUTOCLAIM enable automatic re-delivery of
    commands that were consumed but not ACK'd (e.g. due to worker restart).

Stream message format (each Redis stream entry field) — see contract.py:
  command_json   — JSON-serialised WorkerCommand  (contract v2, preferred)
  — or the legacy v1 flat layout: command_id, command_type, vcp_id, group_id,
    payload (JSON string), issued_by, ts

Consumer groups are created automatically on worker start if absent:
  * per-worker stream  → shared group ``{consumer_group}``
  * broadcast stream   → one group *per worker* ``{consumer_group}:{worker_id}``
    so every worker receives every broadcast (a shared group would deliver
    each broadcast to only one worker).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import redis.asyncio as aioredis

from vcp_data_plane.config.schema import CommandType, RedisConfig, WorkerCommand

logger = logging.getLogger(__name__)

# Dispatch callback: receives a WorkerCommand; returns response dict (ack'd payload)
CommandHandler = Callable[[WorkerCommand], Coroutine[Any, Any, dict[str, Any]]]
# Rejection callback: (raw command dict, reason) — used to report unusable commands
RejectHandler = Callable[[dict[str, Any], str], Coroutine[Any, Any, None]]


def parse_command_fields(fields: dict[str, str]) -> dict[str, Any] | None:
    """Decode a stream entry into a raw command dict (v2 or legacy v1 layout).

    Returns None when the entry carries no command at all.
    """
    raw_json = fields.get("command_json")
    if raw_json:
        return json.loads(raw_json)
    if not fields.get("command_type"):
        return None
    # Legacy v1 flat layout
    payload_raw = fields.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        payload = {}
    return {
        "command_id": fields.get("command_id", ""),
        "command_type": fields.get("command_type", ""),
        "vcp_id": fields.get("vcp_id") or None,
        "group_id": fields.get("group_id") or None,
        "payload": payload if isinstance(payload, dict) else {},
        "issued_by": fields.get("issued_by", ""),
        "issued_at": fields.get("issued_at") or fields.get("ts", ""),
    }


class CommandListener:
    """Listens to Redis Streams and dispatches commands to registered handlers."""

    def __init__(
        self,
        worker_id: str,
        redis_config: RedisConfig,
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._cfg = redis_config
        self._redis: aioredis.Redis | None = redis_client
        self._owns_redis = redis_client is None
        self._handlers: dict[CommandType, CommandHandler] = {}
        self._on_reject: RejectHandler | None = None
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._consumer_name = f"consumer-{worker_id}"

    @property
    def broadcast_group(self) -> str:
        """Per-worker consumer group on the broadcast stream."""
        return f"{self._cfg.consumer_group}:{self._worker_id}"

    def on_reject(self, handler: RejectHandler) -> None:
        """Register a callback for commands that cannot be executed."""
        self._on_reject = handler

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register(self, command_type: CommandType, handler: CommandHandler) -> None:
        """Register an async handler for a command type."""
        self._handlers[command_type] = handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        cfg = self._cfg
        if self._redis is None:
            self._redis = aioredis.Redis(
                host=cfg.host,
                port=cfg.port,
                db=cfg.db,
                password=cfg.password,
                ssl=cfg.ssl,
                decode_responses=True,
            )
        await self._redis.ping()

        worker_stream = f"{cfg.command_stream_prefix}:{self._worker_id}"
        broadcast_stream = cfg.broadcast_stream

        # Create consumer groups if they don't exist.  Targeted commands queued
        # before the worker started are delivered ("0"); broadcasts are only
        # delivered from the moment this worker joins ("$").
        await self._ensure_group(worker_stream, cfg.consumer_group, start_id="0")
        await self._ensure_group(broadcast_stream, self.broadcast_group, start_id="$")

        self._running = True
        # Start separate reader tasks for each stream
        self._tasks = [
            asyncio.create_task(
                self._read_loop(worker_stream, cfg.consumer_group),
                name=f"cmd-worker-{self._worker_id}",
            ),
            asyncio.create_task(
                self._read_loop(broadcast_stream, self.broadcast_group),
                name=f"cmd-broadcast-{self._worker_id}",
            ),
        ]
        logger.info("CommandListener started — streams: %s, %s", worker_stream, broadcast_stream)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._redis and self._owns_redis:
            await self._redis.aclose()
        logger.info("CommandListener stopped")

    # ------------------------------------------------------------------
    # Stream read loop
    # ------------------------------------------------------------------

    async def _read_loop(self, stream: str, group: str) -> None:
        """Blocking read loop for a single Redis Stream consumer group."""
        cfg = self._cfg
        block_ms = cfg.stream_block_ms

        while self._running:
            try:
                # XREADGROUP: block until a message arrives (or timeout)
                entries = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=self._consumer_name,
                    streams={stream: ">"},
                    count=10,               # Process up to 10 commands per iteration
                    block=block_ms,
                )

                if not entries:
                    continue

                for stream_key, messages in entries:
                    for msg_id, fields in messages:
                        await self._handle_entry(stream_key, group, msg_id, fields)

                # Also claim timed-out pending entries (PEL) to handle crashed workers
                await self._reclaim_pending(stream, group)

            except asyncio.CancelledError:
                break
            except aioredis.ConnectionError as exc:
                logger.error("Redis connection error in command listener: %s", exc)
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Command listener error: %s", exc)
                await asyncio.sleep(1)

    async def _handle_entry(self, stream: str, group: str, msg_id: str, fields: dict[str, str]) -> None:
        """Parse and dispatch a single stream entry."""
        try:
            data = parse_command_fields(fields)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Undecodable command in stream %s msg_id=%s: %s", stream, msg_id, exc)
            await self._ack(stream, group, msg_id)
            return
        if data is None:
            await self._ack(stream, group, msg_id)
            return

        try:
            command = WorkerCommand.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rejected command in stream %s msg_id=%s: %s", stream, msg_id, exc)
            ct = data.get("command_type")
            if ct not in {c.value for c in CommandType}:
                reason = f"unsupported command_type {ct!r}"
            else:
                reason = f"invalid command: {exc}".splitlines()[0]
            await self._reject(data, reason)
            await self._ack(stream, group, msg_id)
            return

        command._via_broadcast = stream == self._cfg.broadcast_stream

        handler = self._handlers.get(command.command_type)
        if handler is None:
            logger.debug("No handler for command_type=%s — ACK'd without action", command.command_type)
            await self._reject(data, f"no handler for {command.command_type.value!r}")
            await self._ack(stream, group, msg_id)
            return

        try:
            result = await handler(command)
            logger.debug(
                "Command %s [%s] dispatched → %s",
                command.command_type, command.command_id, result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Command handler error for %s [%s]: %s",
                command.command_type, command.command_id, exc,
            )
        finally:
            # Always ACK — we don't want dead commands to block the stream
            await self._ack(stream, group, msg_id)

    async def _reject(self, data: dict[str, Any], reason: str) -> None:
        if self._on_reject is None:
            return
        try:
            await self._on_reject(data, reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Reject callback failed: %s", exc)

    async def _ack(self, stream: str, group: str, msg_id: str) -> None:
        try:
            await self._redis.xack(stream, group, msg_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("XACK failed for %s: %s", msg_id, exc)

    async def _reclaim_pending(self, stream: str, group: str) -> None:
        """Auto-claim pending messages older than 30s (worker that consumed them may have died)."""
        try:
            # XAUTOCLAIM: steal messages pending > 30000ms
            claimed = await self._redis.xautoclaim(
                name=stream,
                groupname=group,
                consumername=self._consumer_name,
                min_idle_time=30_000,
                start_id="0-0",
                count=5,
            )
            messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) > 1 else []
            for msg_id, fields in messages:
                logger.info("Reclaimed pending message %s from stream %s", msg_id, stream)
                await self._handle_entry(stream, group, msg_id, fields)
        except Exception:  # noqa: BLE001
            pass  # XAUTOCLAIM not available on older Redis; silently skip

    async def _ensure_group(self, stream: str, group: str, start_id: str = "0") -> None:
        """Create consumer group and stream if they don't exist."""
        try:
            await self._redis.xgroup_create(stream, group, id=start_id, mkstream=True)
            logger.info("Created consumer group '%s' on stream '%s'", group, stream)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                pass  # Group already exists — normal
            elif "ERR The XGROUP subcommand requires the key to exist" in str(exc):
                # Stream doesn't exist yet, mkstream handles it
                pass
            else:
                raise


class CommandPublisher:
    """Publishes WorkerCommands to Redis Streams (used by control plane / API)."""

    def __init__(self, redis_config: RedisConfig) -> None:
        self._cfg = redis_config
        self._redis: aioredis.Redis | None = None

    async def start(self) -> None:
        cfg = self._cfg
        self._redis = aioredis.Redis(
            host=cfg.host,
            port=cfg.port,
            db=cfg.db,
            password=cfg.password,
            ssl=cfg.ssl,
            decode_responses=True,
        )

    async def stop(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def publish(self, worker_id: str, command: WorkerCommand) -> str:
        """Publish a command to a specific worker's stream. Returns stream message ID."""
        stream = f"{self._cfg.command_stream_prefix}:{worker_id}"
        msg_id = await self._redis.xadd(
            stream,
            {"command_json": command.model_dump_json()},
            maxlen=10_000,      # Trim stream to avoid unbounded growth
            approximate=True,
        )
        logger.debug("Published command %s to %s → msg_id=%s", command.command_type, stream, msg_id)
        return msg_id

    async def broadcast(self, command: WorkerCommand) -> str:
        """Publish a command to the broadcast stream (all workers consume it)."""
        msg_id = await self._redis.xadd(
            self._cfg.broadcast_stream,
            {"command_json": command.model_dump_json()},
            maxlen=5_000,
            approximate=True,
        )
        logger.debug("Broadcast command %s → msg_id=%s", command.command_type, msg_id)
        return msg_id
