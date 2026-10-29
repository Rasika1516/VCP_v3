"""Shared pytest fixtures for the VCP Control Plane test suite."""
from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Environment — point tests at SQLite (in-memory) and fake Redis
# ---------------------------------------------------------------------------

os.environ.setdefault("VCP_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("VCP_CP_API_TOKEN", "test-token")
os.environ.setdefault("VCP_CP_REQUIRE_AUTH", "true")
os.environ.setdefault("VCP_CP_REDIS_HOST", "localhost")
os.environ.setdefault("VCP_CP_WORKER_URLS_RAW", "")
os.environ.setdefault("VCP_CP_FRONTEND_URL", "http://localhost:5173")

from vcp_control_plane.db.models import Base  # noqa: E402 — after env setup

TEST_ENGINE = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
)
TestSessionFactory = async_sessionmaker(TEST_ENGINE, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """Create all tables in the in-memory SQLite DB once per session."""
    async with TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional DB session that is rolled back after each test."""
    async with TEST_ENGINE.connect() as conn:
        await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


@pytest.fixture()
def fake_redis():
    """Return a fakeredis async client."""
    try:
        import fakeredis.aioredis as fake_aioredis
        r = fake_aioredis.FakeRedis(decode_responses=True)
        return r
    except ImportError:
        pytest.skip("fakeredis not installed")


@pytest_asyncio.fixture()
async def client(db_session, fake_redis) -> AsyncGenerator[AsyncClient, None]:
    """HTTPX async test client wired to the FastAPI app with overridden dependencies."""
    from vcp_control_plane.api.app import create_app
    from vcp_control_plane.api.deps import get_redis, DbSession
    from vcp_control_plane.db.base import get_db_session

    app = create_app()

    # Override DB and Redis dependencies
    async def override_db():
        yield db_session

    def override_redis():
        return fake_redis

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_redis] = override_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


TEST_PROFILE = {
    "profileId": "test-profile",
    "vendor": "TestVendor",
    "model": "T1",
    "ocppVersion": "1.6J",
    "evseDefinition": [{"evse_id": 1, "connectors": [{"connector_id": 1}]}],
}


@pytest_asyncio.fixture()
async def fleet_id(client) -> str:
    """Create a profile + a 2-VCP draft fleet; returns the fleet ID."""
    r = await client.post("/api/v1/profiles", json=TEST_PROFILE)
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/fleets", json={
        "name": "Test Fleet",
        "csmsUrl": "ws://csms.test:9000/ocpp",
        "profileId": "test-profile",
        "vcpCount": 2,
        "idPrefix": "TF-",
        "chargePointIdPrefix": "TFCP-",
        "groupId": "grp-fleet",
    })
    assert r.status_code == 201, r.text
    return r.json()["fleetId"]
