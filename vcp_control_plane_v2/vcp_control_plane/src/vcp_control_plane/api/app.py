"""FastAPI application factory for the VCP Control Plane."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vcp_control_plane.api.deps import set_redis_client
from vcp_control_plane.api.routers import commands, events, fleet, fleets, profiles, scenarios, vcps
from vcp_control_plane.bus.subscriber import EventSubscriber
from vcp_control_plane.config.settings import get_settings
from vcp_control_plane.db.base import dispose_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    settings = get_settings()

    # --- Redis ---
    redis_client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        ssl=settings.redis_ssl,
        db=settings.redis_db,
        decode_responses=True,
    )
    set_redis_client(redis_client)
    logger.info("Redis client connected to %s:%d", settings.redis_host, settings.redis_port)

    # --- Event subscriber (background task: reads worker events → DB + WS) ---
    subscriber = EventSubscriber(redis_client=redis_client, settings=settings)
    app.state.event_subscriber = subscriber
    import asyncio
    sub_task = asyncio.create_task(subscriber.run(), name="event-subscriber")

    # --- Fleet monitor (marks fleets whose worker died as failed) ---
    monitor_task = asyncio.create_task(_fleet_monitor(settings), name="fleet-monitor")

    logger.info("VCP Control Plane ready — listening on %s:%d", settings.api_host, settings.api_port)
    yield

    # --- Shutdown ---
    # Deployed workers keep running (they run in their own session / container);
    # the monitor re-attaches to them on the next start.
    for task in (sub_task, monitor_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await redis_client.aclose()
    await dispose_engine()
    logger.info("VCP Control Plane shut down cleanly")


async def _fleet_monitor(settings, interval_s: float = 5.0) -> None:
    import asyncio

    from vcp_control_plane.db.base import get_session_factory
    from vcp_control_plane.registry.store import VcpRegistryStore
    from vcp_control_plane.workers.fleet_service import FleetService

    while True:
        try:
            async with get_session_factory()() as session:
                await FleetService(VcpRegistryStore(session), settings).monitor_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("fleet monitor error: %s", exc)
        await asyncio.sleep(interval_s)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="VCP Fleet Simulator — Control & Management Plane",
        version="0.2.0",
        description=(
            "REST + WebSocket API for controlling and monitoring the VCP simulator fleet. "
            "Dispatches commands via Redis Streams to data-plane workers."
        ),
        lifespan=lifespan,
    )

    # CORS — allow the React dev server and any configured frontend origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url, "http://localhost:5173", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Routers ---
    app.include_router(vcps.router, prefix="/api/v1", tags=["VCPs"])
    app.include_router(fleet.router, prefix="/api/v1", tags=["Fleet"])
    app.include_router(fleets.router, prefix="/api/v1", tags=["VCP Fleets"])
    app.include_router(commands.router, prefix="/api/v1", tags=["Commands"])
    app.include_router(profiles.router, prefix="/api/v1", tags=["Profiles"])
    app.include_router(scenarios.router, prefix="/api/v1", tags=["Scenarios"])
    app.include_router(events.router, prefix="/api/v1", tags=["Events"])

    @app.get("/healthz", tags=["Health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "vcp-control-plane"}

    return app
