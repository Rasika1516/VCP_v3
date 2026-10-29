"""FastAPI dependencies: DB session, Redis client, bus publisher."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from vcp_control_plane.config.settings import Settings, get_settings
from vcp_control_plane.db.base import get_db_session

# Re-export session dep with type alias for cleaner router imports
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _make_redis_client(settings: Settings) -> aioredis.Redis:
    return aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        ssl=settings.redis_ssl,
        db=settings.redis_db,
        decode_responses=True,
    )


# Module-level Redis client — created once at startup
_redis_client: aioredis.Redis | None = None


def set_redis_client(client: aioredis.Redis) -> None:
    global _redis_client
    _redis_client = client


def get_redis() -> aioredis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis client not initialised — call set_redis_client() at startup")
    return _redis_client


RedisClient = Annotated[aioredis.Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
