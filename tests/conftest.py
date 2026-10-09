"""Shared fixtures.

The database tests run against the real Postgres from docker-compose, not a
mock. What they check -- row counts, topic clusters, sequence state -- is
produced by Postgres itself, so a fake would only re-assert the fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


@pytest.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    """A connection to the seeded database.

    Skips rather than fails when Postgres is not up, so `pytest -q` on a clean
    checkout reports "skipped, needs docker compose up" instead of a wall of
    connection errors that look like real breakage.
    """
    try:
        conn: asyncpg.Connection = await asyncpg.connect(settings.postgres_dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"Postgres unreachable at {settings.postgres_host}:"
            f"{settings.postgres_port} ({exc}). Run: docker compose up postgres redis"
        )

    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Drives the FastAPI app in-process over ASGI.

    ASGITransport calls the app directly, so there is no uvicorn process, no
    port to bind, and no network. That keeps the API tests fast and makes them
    independent of whether anything is already running on 8000.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client
