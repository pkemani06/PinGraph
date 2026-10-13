"""asyncpg connection pool and every SQL query the app runs.

All SQL lives here rather than in the route handlers, so the queries are in one
place to read and tune, and the rest of the app never holds a raw connection.

The pool is module state, created once in the Phase 3 lifespan and closed on
shutdown. A pool rather than a connection per request because each new asyncpg
connection costs a TCP handshake plus a Postgres backend fork -- tens of
milliseconds, which would dwarf the queries below.
"""

from __future__ import annotations

import asyncpg

from app.config import settings

# Sized for the Phase 6 layout: 3 API replicas against one Postgres. 10 each
# leaves room under the default max_connections of 100 for psql and scripts.
MIN_POOL_SIZE = 2
MAX_POOL_SIZE = 10

_pool: asyncpg.Pool | None = None


async def create_pool(dsn: str | None = None) -> asyncpg.Pool:
    """Open the pool. Called once at startup, before anything queries."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn or settings.postgres_dsn,
        min_size=MIN_POOL_SIZE,
        max_size=MAX_POOL_SIZE,
    )
    return _pool


async def close_pool() -> None:
    """Close the pool and wait for in-flight queries to finish."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """The open pool, or a clear error instead of an AttributeError deep in a query."""
    if _pool is None:
        raise RuntimeError("database pool not initialised; call create_pool() first")
    return _pool


async def fetch_all_edges() -> list[tuple[int, int]]:
    """Every save in the database, as (board_id, pin_id) pairs.

    This is the whole graph, read once at startup to build the in-memory
    adjacency lists. No ORDER BY: the graph is a dict of lists, so load order
    does not matter, and sorting 200k rows would only add work. Returns plain
    tuples rather than Records so app/graph.py does not depend on asyncpg.
    """
    rows = await get_pool().fetch("SELECT board_id, pin_id FROM board_pins")
    return [(row["board_id"], row["pin_id"]) for row in rows]


async def insert_edge(board_id: int, pin_id: int) -> bool:
    """Save a pin to a board. True if this was a new edge, False if it existed.

    ON CONFLICT DO NOTHING because (board_id, pin_id) is the primary key and
    re-saving a pin is a normal user action, not an error. The caller needs the
    distinction: Phase 5 should only publish to the other replicas when the edge
    is genuinely new, otherwise a double-click fans out a redundant message.
    """
    inserted = await get_pool().fetchval(
        """
        INSERT INTO board_pins (board_id, pin_id)
        VALUES ($1, $2)
        ON CONFLICT (board_id, pin_id) DO NOTHING
        RETURNING pin_id
        """,
        board_id,
        pin_id,
    )
    return inserted is not None


async def recent_pins_for_user(user_id: int, limit: int = 20) -> list[int]:
    """The user's most recently saved pin ids, newest first.

    These are the seeds for the Phase 3 feed: run related_pins from each and
    merge. GROUP BY pin_id collapses a pin saved to several of the user's own
    boards into one row, ordered by its latest save -- without that, one pin
    could take several of the `limit` slots and narrow the feed.
    """
    rows = await get_pool().fetch(
        """
        SELECT bp.pin_id
        FROM board_pins bp
        JOIN boards b ON b.id = bp.board_id
        WHERE b.user_id = $1
        GROUP BY bp.pin_id
        ORDER BY max(bp.created_at) DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [row["pin_id"] for row in rows]
