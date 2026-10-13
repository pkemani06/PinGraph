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

import app.db as app_db
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


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """app.db's own module-level pool, opened and closed around one test.

    app/db.py holds the pool in a module global, so a test that left it open
    would leak into the next one. Opening and closing it here means every test
    starts from the same state -- _pool is None -- which is what lets the
    uninitialised-pool test assert on that case no matter what order the tests
    run in.
    """
    try:
        created = await app_db.create_pool()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"Postgres unreachable at {settings.postgres_host}:"
            f"{settings.postgres_port} ({exc}). Run: docker compose up postgres redis"
        )

    try:
        yield created
    finally:
        await app_db.close_pool()


@pytest.fixture
async def scratch_edge(db: asyncpg.Connection) -> AsyncIterator[tuple[int, int]]:
    """A (board_id, pin_id) pair that has no edge yet, removed again afterwards.

    insert_edge() writes to the same seeded database that tests/test_seed.py
    asserts exact row counts against, and it takes its own connection from the
    pool -- so wrapping the test in a transaction on this connection could not
    contain the write. Instead: find a pair that genuinely has no edge, let the
    test create it, and delete it in teardown. That restores the row count
    exactly, so the two test modules stay independent of execution order.
    """
    pair = await db.fetchrow(
        """
        SELECT b.id AS board_id, p.id AS pin_id
        FROM boards b, pins p
        WHERE b.id = (SELECT min(id) FROM boards)
          AND NOT EXISTS (
              SELECT 1 FROM board_pins bp
              WHERE bp.board_id = b.id AND bp.pin_id = p.id
          )
        ORDER BY p.id
        LIMIT 1
        """
    )
    assert pair is not None, "no free (board, pin) pair: every pin is on board 1"

    board_id, pin_id = pair["board_id"], pair["pin_id"]
    try:
        yield board_id, pin_id
    finally:
        await db.execute(
            "DELETE FROM board_pins WHERE board_id = $1 AND pin_id = $2",
            board_id,
            pin_id,
        )
