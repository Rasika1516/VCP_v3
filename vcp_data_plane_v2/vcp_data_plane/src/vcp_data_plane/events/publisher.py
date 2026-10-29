"""Worker → Control Plane event publisher (contract v2).

Writes events to ``{event_stream_prefix}:{worker_id}`` (default
``vcp:events:worker:{worker_id}``), the stream the control plane's
EventSubscriber consumes.  See ``vcp_data_plane.contract`` for the field
layout and event vocabulary.

Publishing is best-effort: a Redis hiccup is logged at debug level and never
propagates into the VCP runtime (a simulator must not stall its OCPP session
because the observability path is down).
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


class EventPublisher:
    """Publishes worker events to the control-plane event stream."""

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
        self.published = 0

    @property
    def stream(self) -> str:
        return f"{self._cfg.event_stream_prefix}:{self._worker_id}"

    async def start(self) -> None:
        if self._redis is None:
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
        if self._redis is not None and self._owns_redis:
            await self._redis.aclose()

    async def publish(self, event: str, vcp_id: str = "", payload: dict[str, Any] | None = None) -> None:
        if self._redis is None:
            return
        entry = {
            "event": event,
            "vcp_id": vcp_id or "",
            "worker_id": self._worker_id,
            "payload": json.dumps(payload or {}, default=str),
            "ts": _now_iso(),
        }
        try:
            await self._redis.xadd(
                self.stream,
                entry,
                maxlen=self._cfg.event_maxlen,
                approximate=True,
            )
            self.published += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("Event publish failed (%s/%s): %s", event, vcp_id, exc)


class NullEventPublisher(EventPublisher):
    """No-op publisher (tests / Redis-less runs)."""

    def __init__(self) -> None:  # noqa: D401 — intentionally skips base init
        self._worker_id = ""
        self._redis = None
        self.published = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def publish(self, event: str, vcp_id: str = "", payload: dict[str, Any] | None = None) -> None:
        pass
