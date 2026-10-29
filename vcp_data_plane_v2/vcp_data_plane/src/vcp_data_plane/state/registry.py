"""VCP Instance Registry.

Maintains the in-memory mapping of vcp_id → VcpStateMachine and publishes
state snapshots to Redis so the control plane can query any VCP's state
without querying individual workers.

Redis key layout:
  HASH  vcp:state:{vcp_id}         — serialised snapshot fields
  HASH  vcp:registry               — vcp_id → worker_id mapping
  SET   vcp:worker:{worker_id}     — set of vcp_ids owned by this worker
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from vcp_data_plane.config.schema import RedisConfig
from vcp_data_plane.state.machine import VcpState, VcpStateMachine

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class VcpRegistry:
    """In-memory VCP registry with Redis-backed persistence."""

    def __init__(
        self,
        worker_id: str,
        redis_config: RedisConfig,
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._redis_cfg = redis_config
        self._machines: dict[str, VcpStateMachine] = {}
        self._redis: aioredis.Redis | None = redis_client
        self._owns_redis = redis_client is None

    async def start(self) -> None:
        cfg = self._redis_cfg
        if self._redis is not None:
            await self._redis.ping()
            return
        self._redis = aioredis.Redis(
            host=cfg.host,
            port=cfg.port,
            db=cfg.db,
            password=cfg.password,
            ssl=cfg.ssl,
            decode_responses=True,
        )
        # Verify connection
        await self._redis.ping()
        logger.info("VcpRegistry connected to Redis %s:%d", cfg.host, cfg.port)

    async def stop(self) -> None:
        if self._redis and self._owns_redis:
            await self._redis.aclose()
        self._redis = None

    def register(self, machine: VcpStateMachine) -> None:
        """Add a VcpStateMachine to the in-memory registry."""
        self._machines[machine._vcp_id] = machine  # noqa: SLF001

    def get(self, vcp_id: str) -> VcpStateMachine | None:
        return self._machines.get(vcp_id)

    def all(self) -> dict[str, VcpStateMachine]:
        return dict(self._machines)

    def count(self) -> int:
        return len(self._machines)

    async def publish_state(self, vcp_id: str) -> None:
        """Write the VCP's current state snapshot to Redis."""
        if self._redis is None:
            return
        machine = self._machines.get(vcp_id)
        if machine is None:
            return
        snapshot = machine.snapshot()
        key = f"{self._redis_cfg.state_hash_prefix}:{vcp_id}"
        try:
            # Flatten snapshot for Redis HASH storage
            flat = {
                "worker_id": self._worker_id,
                "connection": snapshot["connection"],
                "connectors_json": json.dumps(snapshot["connectors"]),
            }
            await self._redis.hset(key, mapping=flat)
            # Also update the global registry
            await self._redis.hset(self._redis_cfg.registry_key, vcp_id, self._worker_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Redis state publish failed for %s: %s", vcp_id, exc)

    async def register_worker(self) -> None:
        """Announce this worker and its VCP set in Redis."""
        if self._redis is None:
            return
        worker_key = f"vcp:worker:{self._worker_id}"
        pipe = self._redis.pipeline()
        for vcp_id in self._machines:
            pipe.sadd(worker_key, vcp_id)
            pipe.hset(self._redis_cfg.registry_key, vcp_id, self._worker_id)
        await pipe.execute()
        logger.info("Worker %s registered %d VCPs in Redis", self._worker_id, len(self._machines))

    async def deregister_worker(self) -> None:
        """Remove this worker's VCP assignments from Redis on graceful shutdown."""
        if self._redis is None:
            return
        worker_key = f"vcp:worker:{self._worker_id}"
        pipe = self._redis.pipeline()
        for vcp_id in self._machines:
            pipe.hdel(self._redis_cfg.registry_key, vcp_id)
        pipe.delete(worker_key)
        await pipe.execute()
        logger.info("Worker %s deregistered from Redis", self._worker_id)

    def summary(self) -> dict[str, int]:
        """Count VCPs by state for the health API."""
        counts: dict[str, int] = {s.value: 0 for s in VcpState}
        for machine in self._machines.values():
            state = machine.primary.state.value
            counts[state] = counts.get(state, 0) + 1
        return counts
