"""Command Publisher — writes WorkerCommand messages to Redis Streams.

Stream key conventions (must match vcp_data_plane exactly — see bus/contract.py):

  Per-worker:   vcp:commands:worker:{worker_id}
  Broadcast:    vcp:commands:broadcast

Contract v2 entry fields:

  command_json  — JSON-encoded WorkerCommand:
                  {command_id, command_type, vcp_id, group_id, payload,
                   issued_by, issued_at}
  command_type  — duplicated for readability in redis-cli / monitoring
  vcp_id        — duplicated for readability ("" for broadcast)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from vcp_control_plane.config.settings import Settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_command_fields(
    command_id: str,
    command_type: str,
    payload: dict,
    vcp_id: str | None = None,
    group_id: str | None = None,
    issued_by: str | None = None,
) -> dict[str, str]:
    command = {
        "command_id": command_id,
        "command_type": command_type,
        "vcp_id": vcp_id or None,
        "group_id": group_id or None,
        "payload": payload or {},
        "issued_by": issued_by or "",
        "issued_at": _now_iso(),
    }
    return {
        "command_json": json.dumps(command, default=str),
        "command_type": command_type,
        "vcp_id": vcp_id or "",
    }


class CommandPublisher:
    def __init__(self, redis_client: aioredis.Redis, settings: Settings) -> None:
        self._redis = redis_client
        self._settings = settings

    def _worker_stream(self, worker_id: str) -> str:
        return f"{self._settings.command_stream_prefix}:{worker_id}"

    @property
    def _broadcast_stream(self) -> str:
        return self._settings.broadcast_stream

    async def send_to_vcp(
        self,
        vcp_id: str,
        worker_id: str | None,
        command_type: str,
        payload: dict,
        command_id: str,
        issued_by: str | None = None,
        group_id: str | None = None,
    ) -> str:
        """Publish a command targeting a specific VCP.

        If worker_id is known, the command goes to the worker's dedicated stream.
        If not, it falls through to the broadcast stream; every worker reads it
        and only the worker that owns ``vcp_id`` executes it.
        """
        fields = build_command_fields(command_id, command_type, payload, vcp_id, group_id, issued_by)
        stream = self._worker_stream(worker_id) if worker_id else self._broadcast_stream
        msg_id = await self._redis.xadd(stream, fields, maxlen=10_000, approximate=True)
        logger.debug("Published command %s (%s) → %s [msg=%s]", command_id, command_type, stream, msg_id)
        return msg_id

    async def broadcast(
        self,
        command_type: str,
        payload: dict,
        command_id: str,
        issued_by: str | None = None,
        group_id: str | None = None,
    ) -> str:
        """Publish a fleet-wide (group_id None) or group-scoped broadcast command."""
        fields = build_command_fields(command_id, command_type, payload, None, group_id, issued_by)
        msg_id = await self._redis.xadd(self._broadcast_stream, fields, maxlen=10_000, approximate=True)
        logger.debug(
            "Broadcast command %s (%s, group=%s) → %s [msg=%s]",
            command_id, command_type, group_id, self._broadcast_stream, msg_id,
        )
        return msg_id
