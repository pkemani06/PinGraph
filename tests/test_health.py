"""Tests for GET /health.

Thin by design. /health deliberately checks nothing external (see
app/routes/health.py), so the only thing worth asserting is that the route is
mounted and the process can serve a response. nginx uses it as a liveness
probe in Phase 6; a health check that queried Postgres would take replicas out
of the pool for a database problem they cannot fix by restarting.
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_unknown_path_returns_404(client: AsyncClient) -> None:
    """Guards against a future router being mounted with a catch-all prefix."""
    response = await client.get("/not-a-route")

    assert response.status_code == 404
