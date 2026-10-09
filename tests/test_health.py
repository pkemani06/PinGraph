"""Tests for GET /health.

The endpoint is a liveness probe: it reports that this process is serving, and
checks nothing external on purpose. These tests drive the app in-process through
httpx's ASGI transport, with no server and no database running, which is also
the point -- if /health ever grew a dependency on Postgres or Redis, this file
would stop passing.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client wired straight to the ASGI app, no network involved."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_rejects_non_get(client: httpx.AsyncClient) -> None:
    """nginx probes with GET (Phase 6). Anything else is not a health check."""
    response = await client.post("/health")

    assert response.status_code == 405
