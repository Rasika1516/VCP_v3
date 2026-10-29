"""OCPP Trace Publisher.

Captures every OCPP message (sent and received) for each VCP and publishes
it to a Redis Stream for real-time observation by the control plane UI.

Stream key: vcp:trace:{vcp_id}

Each stream entry contains:
  direction    — "→" (VCP→CSMS) or "←" (CSMS→VCP)
  action       — OCPP action name (BootNotification, etc.)
  message_id   — OCPP message_id
  payload_json — JSON payload for the message
  ts           — ISO8601 timestamp
  vcp_id       — VCP identifier
  worker_id    — Worker identifier

The stream is trimmed to trace_maxlen entries (per config) to prevent
unbounded growth.  For longer retention, an external consumer can drain
the stream to persistent storage (Postgres, S3, etc.).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from vcp_data_plane.config.schema import RedisConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TracePublisher:
    """Publishes OCPP traces to Redis Streams."""

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
        self._enabled = True

    async def start(self) -> None:
        if self._redis is not None:
            return
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
        if self._redis and self._owns_redis:
            await self._redis.aclose()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    async def trace_sent(self, vcp_id: str, action: str, message_id: str, payload: dict[str, Any]) -> None:
        """Record an outgoing OCPP message (VCP → CSMS)."""
        await self._publish(vcp_id, "→", action, message_id, payload)

    async def trace_received(self, vcp_id: str, action: str, message_id: str, payload: dict[str, Any]) -> None:
        """Record an incoming OCPP message (CSMS → VCP)."""
        await self._publish(vcp_id, "←", action, message_id, payload)

    async def trace_error(self, vcp_id: str, error_code: str, description: str, message_id: str = "") -> None:
        """Record an OCPP CALLERROR."""
        await self._publish(vcp_id, "✗", f"ERROR:{error_code}", message_id, {"description": description})

    async def _publish(
        self,
        vcp_id: str,
        direction: str,
        action: str,
        message_id: str,
        payload: dict[str, Any],
    ) -> None:
        if not self._enabled or self._redis is None:
            return
        stream_key = f"{self._cfg.trace_stream_prefix}:{vcp_id}"
        entry = {
            "direction": direction,
            "action": action,
            "message_id": message_id,
            "payload_json": json.dumps(payload),
            "ts": _now_iso(),
            "vcp_id": vcp_id,
            "worker_id": self._worker_id,
        }
        try:
            await self._redis.xadd(
                stream_key,
                entry,
                maxlen=self._cfg.trace_maxlen,
                approximate=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Trace publish failed for %s: %s", vcp_id, exc)


class NullTracePublisher(TracePublisher):
    """No-op publisher for testing without Redis."""

    def __init__(self) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def trace_sent(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def trace_received(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def trace_error(self, *args: Any, **kwargs: Any) -> None:
        pass
