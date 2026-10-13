"""Tests for app/db.py: the pool's lifecycle and the three queries on it.

These run against the real Postgres from docker-compose, like tests/test_seed.py,
and for the same reason: what is worth asserting here is behaviour Postgres
provides -- ON CONFLICT, GROUP BY, index-backed ordering -- so a mock would only
re-assert the mock.

What test_seed.py covers is the *data*; this covers the *access layer* over it.
The split matters because Phase 2 onwards reads the graph exclusively through
these functions, and two of their contracts are load-bearing later:
fetch_all_edges() returning plain tuples is what keeps app/graph.py free of an
asyncpg dependency, and insert_edge() returning False on a duplicate is what
stops Phase 5 from fanning out a redundant pub/sub message on a double-click.
"""

from __future__ import annotations

import asyncpg
import pytest

import app.db as app_db
from app.db import MAX_POOL_SIZE, MIN_POOL_SIZE


async def _lowest_user_id(db: asyncpg.Connection) -> int:
    """Any user that owns boards. Queried rather than hardcoded to 1 so the
    tests survive a reseed that changes the id space."""
    return await db.fetchval("SELECT min(user_id) FROM boards")


# --- Pool lifecycle -----------------------------------------------------------


async def test_get_pool_raises_before_the_pool_is_created() -> None:
    """The guard in get_pool() exists so a missing create_pool() in the Phase 3
    lifespan fails with one clear message, instead of an AttributeError on None
    surfacing from inside whichever query happened to run first.

    Deliberately does not use the `pool` fixture, and closes first, so it
    asserts on the uninitialised state regardless of test execution order.
    """
    await app_db.close_pool()

    with pytest.raises(RuntimeError, match="not initialised"):
        app_db.get_pool()


async def test_get_pool_returns_the_pool_create_pool_opened(
    pool: asyncpg.Pool,
) -> None:
    """One pool per process, not a new one per caller."""
    assert app_db.get_pool() is pool


async def test_pool_is_sized_for_three_replicas_against_one_postgres(
    pool: asyncpg.Pool,
) -> None:
    """The Phase 6 layout is 3 API replicas sharing one Postgres, whose default
    max_connections is 100. The ceiling has to leave headroom for psql and
    scripts/seed.py, or a replica restart during a load test fails to connect.
    """
    assert pool.get_min_size() == MIN_POOL_SIZE
    assert pool.get_max_size() == MAX_POOL_SIZE
    assert 3 * MAX_POOL_SIZE < 100


async def test_close_pool_can_be_called_twice(pool: asyncpg.Pool) -> None:
    """Shutdown paths run more than once (a lifespan that raises after closing,
    a test teardown after an explicit close), so the second call must be a
    no-op rather than an AttributeError on None."""
    await app_db.close_pool()
    await app_db.close_pool()

    with pytest.raises(RuntimeError, match="not initialised"):
        app_db.get_pool()


# --- fetch_all_edges ----------------------------------------------------------


async def test_fetch_all_edges_returns_every_save(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """This is the whole graph, read once at startup. Missing edges here mean a
    silently smaller graph and recommendations that are wrong in a way no later
    test would catch.

    Length is asserted as well as set equality: comparing only sets would hide
    a duplicated row, which would double that edge's weight in the walk.
    """
    expected = {
        (row["board_id"], row["pin_id"])
        for row in await db.fetch("SELECT board_id, pin_id FROM board_pins")
    }

    edges = await app_db.fetch_all_edges()

    assert len(edges) == len(expected)
    assert set(edges) == expected


async def test_fetch_all_edges_returns_plain_tuples_of_ints(
    pool: asyncpg.Pool,
) -> None:
    """app/graph.py consumes this and must not import asyncpg.

    asyncpg.Record indexes like a tuple, so unpacking would work and the
    dependency would go unnoticed until something tried to pickle the graph or
    build it from a non-database source. `type() is tuple` pins the conversion
    in db.py; isinstance() would pass for a Record subclass too.
    """
    edges = await app_db.fetch_all_edges()

    assert edges, "no edges: the database is not seeded"
    first = edges[0]
    assert type(first) is tuple
    board_id, pin_id = first
    assert isinstance(board_id, int)
    assert isinstance(pin_id, int)


# --- insert_edge --------------------------------------------------------------


async def test_insert_edge_creates_the_edge_and_reports_it_as_new(
    pool: asyncpg.Pool, db: asyncpg.Connection, scratch_edge: tuple[int, int]
) -> None:
    """True means "this edge is new", which is Phase 5's signal to publish.

    The row is verified on the fixture's own connection, not through
    fetch_all_edges(), so a bug shared by both functions cannot make this pass.
    """
    board_id, pin_id = scratch_edge

    assert await app_db.insert_edge(board_id, pin_id) is True

    stored = await db.fetchval(
        "SELECT count(*) FROM board_pins WHERE board_id = $1 AND pin_id = $2",
        board_id,
        pin_id,
    )
    assert stored == 1


async def test_insert_edge_is_idempotent_for_an_existing_edge(
    pool: asyncpg.Pool, db: asyncpg.Connection, scratch_edge: tuple[int, int]
) -> None:
    """Re-saving a pin is a normal user action, not an error.

    Two things are asserted because they fail separately: the second call must
    not raise (ON CONFLICT DO NOTHING, so a double-click is not a 500), and it
    must return False so Phase 5 does not publish a redundant message to every
    replica. The row count proves DO NOTHING rather than an upsert.
    """
    board_id, pin_id = scratch_edge
    assert await app_db.insert_edge(board_id, pin_id) is True

    assert await app_db.insert_edge(board_id, pin_id) is False

    stored = await db.fetchval(
        "SELECT count(*) FROM board_pins WHERE board_id = $1 AND pin_id = $2",
        board_id,
        pin_id,
    )
    assert stored == 1


# --- recent_pins_for_user -----------------------------------------------------


async def test_recent_pins_for_user_returns_newest_first(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """These are the Phase 3 feed's seed pins, so "recent" has to mean recent.

    Asserts the returned order is non-increasing in each pin's latest save,
    rather than comparing against a second ORDER BY query. Two pins can share a
    timestamp, and at a tie the two queries could legitimately disagree on
    which comes first -- that would be a flaky test, not a bug.
    """
    user_id = await _lowest_user_id(db)

    pins = await app_db.recent_pins_for_user(user_id, limit=25)

    assert pins
    rows = await db.fetch(
        """
        SELECT bp.pin_id, max(bp.created_at) AS last_save
        FROM board_pins bp
        JOIN boards b ON b.id = bp.board_id
        WHERE b.user_id = $1 AND bp.pin_id = ANY($2::int[])
        GROUP BY bp.pin_id
        """,
        user_id,
        pins,
    )
    last_save = {row["pin_id"]: row["last_save"] for row in rows}
    stamps = [last_save[pin_id] for pin_id in pins]
    assert stamps == sorted(stamps, reverse=True)


async def test_recent_pins_for_user_collapses_a_pin_saved_to_several_boards(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """The reason for the GROUP BY. Without it, a pin a user saved to three of
    their own boards would take three of the feed's seed slots and narrow the
    recommendations to that pin's neighbourhood.

    The limit is set above the user's total save count, so the GROUP BY is the
    only thing that can shorten the result.
    """
    row = await db.fetchrow(
        """
        SELECT b.user_id,
               count(*)                  AS saves,
               count(DISTINCT bp.pin_id) AS distinct_pins
        FROM board_pins bp
        JOIN boards b ON b.id = bp.board_id
        GROUP BY b.user_id
        HAVING count(*) > count(DISTINCT bp.pin_id)
        ORDER BY count(*) - count(DISTINCT bp.pin_id) DESC, b.user_id
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no seeded user saved the same pin to two of their own boards")

    pins = await app_db.recent_pins_for_user(row["user_id"], limit=row["saves"] + 1)

    assert len(pins) == row["distinct_pins"] < row["saves"]
    assert len(set(pins)) == len(pins)


async def test_recent_pins_for_user_respects_the_limit(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """The feed runs one walk per seed pin, so the limit is a cap on work per
    request, not a display preference."""
    user_id = await _lowest_user_id(db)

    assert len(await app_db.recent_pins_for_user(user_id, limit=3)) == 3
    assert await app_db.recent_pins_for_user(user_id, limit=0) == []


async def test_recent_pins_for_user_defaults_to_twenty(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """Phase 3 calls this without a limit, so the default is the real one.

    Only meaningful while the user has more than 20 distinct saves; the seed
    gives every user 5 boards of 40 pins, and the assertion says so explicitly
    rather than assuming it.
    """
    user_id = await _lowest_user_id(db)
    distinct_saves = await db.fetchval(
        """
        SELECT count(DISTINCT bp.pin_id)
        FROM board_pins bp
        JOIN boards b ON b.id = bp.board_id
        WHERE b.user_id = $1
        """,
        user_id,
    )
    assert distinct_saves > 20, "user has too few saves for the default to show"

    assert len(await app_db.recent_pins_for_user(user_id)) == 20


async def test_recent_pins_for_user_returns_only_that_users_saves(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """The join is user -> boards -> board_pins. A wrong join key would still
    return plausible pin ids, which is exactly the kind of bug that survives
    eyeballing a feed response."""
    user_id = await _lowest_user_id(db)

    pins = await app_db.recent_pins_for_user(user_id)

    owned = {
        row["pin_id"]
        for row in await db.fetch(
            """
            SELECT bp.pin_id
            FROM board_pins bp
            JOIN boards b ON b.id = bp.board_id
            WHERE b.user_id = $1
            """,
            user_id,
        )
    }
    assert pins
    assert set(pins) <= owned


async def test_recent_pins_for_user_returns_empty_for_an_unknown_user(
    pool: asyncpg.Pool, db: asyncpg.Connection
) -> None:
    """An empty list, not an error: Phase 3 turns this into a 404 for an unknown
    user and an empty feed for a real user who has saved nothing, and it needs
    to make that distinction itself."""
    unknown = await db.fetchval("SELECT max(id) + 1 FROM users")

    assert await app_db.recent_pins_for_user(unknown) == []
