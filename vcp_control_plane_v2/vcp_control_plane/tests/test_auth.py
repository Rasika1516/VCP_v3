"""Tests for bearer-token authentication."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


async def _make_client(app, token=None):
    """Create an httpx client with optional auth header."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


@pytest.mark.asyncio
async def test_no_token_returns_401(db_session, fake_redis) -> None:
    """Request with no Authorization header should return 401."""
    from vcp_control_plane.api.app import create_app
    from vcp_control_plane.db.base import get_db_session
    from vcp_control_plane.api.deps import get_redis

    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_redis] = lambda: fake_redis

    async with await _make_client(app) as c:
        resp = await c.get("/api/v1/vcps")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/vcps", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_passes(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/vcps")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_healthz_is_unauthenticated(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
