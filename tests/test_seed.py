"""Tests that the seeded database matches what scripts/seed.py promises.

Targets are imported from scripts.seed rather than copied here, so retuning a
constant (say PINS_PER_BOARD) moves the data and the assertions together
instead of silently breaking the tests.

These cover the two things Phase 2 depends on. First, the counts: the Phase 1
acceptance criterion is "row counts match targets". Second, and more important,
the *shape*. The recommender's quality metric only means something if boards
really are topic clusters and the graph really is connected enough to walk. If
a reseed quietly produced uniform-random saves, precision@10 would collapse to
the ~5% baseline and the walk would look broken when the data was at fault.
"""

from __future__ import annotations

import asyncpg

from scripts.seed import (
    EDGE_AGE_DAYS,
    IN_TOPIC_FRACTION,
    N_BOARDS,
    N_PINS,
    N_USERS,
    PINS_PER_BOARD,
    PINS_PER_TOPIC,
    TOPICS,
    BOARDS_PER_USER,
)

# What a recommender that ignored the graph would score: one topic in twenty.
RANDOM_TOPIC_BASELINE = 1 / len(TOPICS)


async def test_row_counts_match_seed_targets(db: asyncpg.Connection) -> None:
    counts = await db.fetchrow(
        """
        SELECT (SELECT count(*) FROM users)      AS users,
               (SELECT count(*) FROM pins)       AS pins,
               (SELECT count(*) FROM boards)     AS boards,
               (SELECT count(*) FROM board_pins) AS board_pins
        """
    )

    assert counts["users"] == N_USERS
    assert counts["pins"] == N_PINS
    assert counts["boards"] == N_BOARDS
    assert counts["board_pins"] == N_BOARDS * PINS_PER_BOARD


async def test_every_topic_is_equally_represented(db: asyncpg.Connection) -> None:
    """Even topic sizes keep precision@10 comparable across query pins."""
    rows = await db.fetch("SELECT topic, count(*) AS n FROM pins GROUP BY topic")
    sizes = {row["topic"]: row["n"] for row in rows}

    assert set(sizes) == set(TOPICS)
    assert all(n == PINS_PER_TOPIC for n in sizes.values())


async def test_every_user_owns_the_same_number_of_boards(
    db: asyncpg.Connection,
) -> None:
    extremes = await db.fetchrow(
        """
        SELECT min(n) AS fewest, max(n) AS most
        FROM (SELECT count(*) AS n FROM boards GROUP BY user_id) per_user
        """
    )

    assert extremes["fewest"] == BOARDS_PER_USER
    assert extremes["most"] == BOARDS_PER_USER


async def test_every_board_holds_exactly_the_target_number_of_pins(
    db: asyncpg.Connection,
) -> None:
    """A board short of its target means the dedupe top-up loop gave up early."""
    extremes = await db.fetchrow(
        """
        SELECT min(n) AS fewest, max(n) AS most
        FROM (SELECT count(*) AS n FROM board_pins GROUP BY board_id) per_board
        """
    )

    assert extremes["fewest"] == PINS_PER_BOARD
    assert extremes["most"] == PINS_PER_BOARD


async def test_no_pin_is_isolated(db: asyncpg.Connection) -> None:
    """A pin on no board is unreachable: a walk from it returns nothing, and it
    can never be recommended. Phase 2 tests that case on a hand-built graph on
    purpose; it should not exist by accident in the seeded data."""
    isolated = await db.fetchval(
        """
        SELECT count(*) FROM pins p
        WHERE NOT EXISTS (SELECT 1 FROM board_pins bp WHERE bp.pin_id = p.id)
        """
    )

    assert isolated == 0


async def test_boards_are_topic_clusters(db: asyncpg.Connection) -> None:
    """The ground truth the whole eval rests on.

    For each board, take the share held by its most common topic. Averaged over
    all boards that should sit at or above IN_TOPIC_FRACTION -- the random
    top-up can only ever add to the home topic's share, never subtract. Far
    above the baseline is the part that matters: it means a pin's board
    neighbours carry real topical signal for a walk to pick up.
    """
    shares = await db.fetchrow(
        """
        SELECT avg(share)::float8 AS mean, min(share)::float8 AS lowest
        FROM (
            SELECT board_id, max(n)::numeric / sum(n) AS share
            FROM (
                SELECT bp.board_id, p.topic, count(*) AS n
                FROM board_pins bp
                JOIN pins p ON p.id = bp.pin_id
                GROUP BY bp.board_id, p.topic
            ) per_board_topic
            GROUP BY board_id
        ) per_board
        """
    )

    assert shares["mean"] >= IN_TOPIC_FRACTION
    assert shares["lowest"] > 10 * RANDOM_TOPIC_BASELINE


async def test_saves_are_spread_over_time(db: asyncpg.Connection) -> None:
    """recent_pins_for_user() orders by created_at, so the timestamps have to
    vary. If every save shared one timestamp, "recent" would be arbitrary and
    the Phase 3 feed would return a different answer per query plan."""
    span_days = await db.fetchval(
        """
        SELECT extract(epoch FROM (max(created_at) - min(created_at))) / 86400
        FROM board_pins
        """
    )

    assert span_days > 0.9 * EDGE_AGE_DAYS


async def test_id_sequences_are_past_the_seeded_rows(db: asyncpg.Connection) -> None:
    """COPY writes ids explicitly and leaves the SERIAL sequences untouched, so
    seed.py calls setval afterwards. Without it the first row the API inserts
    would collide with id 1."""
    for table, expected_rows in (
        ("users", N_USERS),
        ("pins", N_PINS),
        ("boards", N_BOARDS),
    ):
        next_id = await db.fetchval(
            """
            SELECT last_value FROM pg_sequences
            WHERE schemaname = 'public'
              AND sequencename = split_part(pg_get_serial_sequence($1, 'id'), '.', 2)
            """,
            table,
        )
        assert next_id >= expected_rows, f"{table} sequence was not advanced"


def _has_btree_on(rows: list[asyncpg.Record], table: str, column: str) -> bool:
    """True when `table` has a single-column btree index led by `column`."""
    return any(
        row["tablename"] == table and row["indexdef"].endswith(f"btree ({column})")
        for row in rows
    )


async def test_schema_has_the_graph_lookup_indexes(db: asyncpg.Connection) -> None:
    """The composite primary key is sorted by board_id, so it serves board ->
    pins only. pin -> boards needs its own index; without it, any direct SQL
    lookup by pin falls back to a full scan of 200k rows."""
    indexed = await db.fetch(
        """
        SELECT tablename, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename IN ('board_pins', 'boards')
        """
    )

    assert _has_btree_on(indexed, "board_pins", "pin_id")
    assert _has_btree_on(indexed, "boards", "user_id")
