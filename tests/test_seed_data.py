"""Checks that scripts/seed.py produced the data the rest of the project assumes.

These run against a live Postgres and are skipped when none is reachable, so
`pytest -q` still passes on a clean clone. Run `docker compose up postgres redis`
and `python scripts/seed.py` first to exercise them.

Expected values are imported from scripts.seed rather than hardcoded, so
reseeding with different tunables moves the assertions with the data instead of
breaking these tests.
"""

from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.config import settings
from scripts.seed import (
    BOARDS_PER_USER,
    EDGE_AGE_DAYS,
    IN_TOPIC_FRACTION,
    N_BOARDS,
    N_PINS,
    N_USERS,
    PINS_PER_BOARD,
    TOPICS,
)

N_EDGES = N_BOARDS * PINS_PER_BOARD


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """A connection to the seeded database, or a skip if it is not up."""
    try:
        connection = await asyncpg.connect(settings.postgres_dsn, timeout=5)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"no Postgres at {settings.postgres_host}: {exc}")

    try:
        yield connection
    finally:
        await connection.close()


async def test_row_counts_match_seed_targets(conn: asyncpg.Connection) -> None:
    counts = await conn.fetchrow(
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
    assert counts["board_pins"] == N_EDGES


async def test_every_pin_has_a_known_topic(conn: asyncpg.Connection) -> None:
    """Topics are the ground truth scripts/eval.py scores against, so a pin with
    a missing or unexpected topic would silently corrupt precision@10."""
    rows = await conn.fetch("SELECT DISTINCT topic FROM pins")

    assert {row["topic"] for row in rows} == set(TOPICS)


async def test_topics_are_evenly_sized(conn: asyncpg.Connection) -> None:
    """Round-robin assignment should give every topic the same pin count. An
    uneven split would bias precision@10 toward whichever topic got the most."""
    rows = await conn.fetch("SELECT count(*) AS n FROM pins GROUP BY topic")

    assert {row["n"] for row in rows} == {N_PINS // len(TOPICS)}


async def test_every_board_holds_the_target_number_of_pins(
    conn: asyncpg.Connection,
) -> None:
    bounds = await conn.fetchrow(
        """
        SELECT min(n) AS lo, max(n) AS hi
        FROM (SELECT count(*) AS n FROM board_pins GROUP BY board_id) AS per_board
        """
    )

    assert bounds["lo"] == PINS_PER_BOARD
    assert bounds["hi"] == PINS_PER_BOARD


async def test_every_user_owns_the_target_number_of_boards(
    conn: asyncpg.Connection,
) -> None:
    bounds = await conn.fetchrow(
        """
        SELECT min(n) AS lo, max(n) AS hi
        FROM (SELECT count(*) AS n FROM boards GROUP BY user_id) AS per_user
        """
    )

    assert bounds["lo"] == BOARDS_PER_USER
    assert bounds["hi"] == BOARDS_PER_USER


async def test_no_pin_is_isolated(conn: asyncpg.Connection) -> None:
    """A walk from a pin with no boards returns nothing, so an isolated pin is an
    unanswerable query. Every seeded pin should have at least one edge."""
    isolated = await conn.fetchval(
        "SELECT count(*) FROM pins p"
        " WHERE NOT EXISTS (SELECT 1 FROM board_pins bp WHERE bp.pin_id = p.id)"
    )

    assert isolated == 0


async def test_boards_are_topic_clustered(conn: asyncpg.Connection) -> None:
    """The whole reason this data exists. Each board should draw most of its pins
    from one topic, otherwise pin -> board -> pin carries no signal and the
    recommender has nothing to find.

    The home topic is not stored on the board (by design -- the recommender must
    not be able to read a label off it), so it is recovered here as the board's
    most common pin topic. The realised share lands slightly above
    IN_TOPIC_FRACTION because the cross-topic top-up draws from the whole
    catalogue, so about 1 in 20 of those lands back in the home topic.
    """
    share = await conn.fetchval(
        """
        WITH per_board_topic AS (
            SELECT bp.board_id,
                   count(*) AS n,
                   row_number() OVER (
                       PARTITION BY bp.board_id ORDER BY count(*) DESC
                   ) AS rank
            FROM board_pins bp
            JOIN pins p ON p.id = bp.pin_id
            GROUP BY bp.board_id, p.topic
        )
        SELECT avg(n) / $1 FROM per_board_topic WHERE rank = 1
        """,
        PINS_PER_BOARD,
    )

    assert float(share) >= IN_TOPIC_FRACTION


async def test_saves_are_spread_over_the_age_window(conn: asyncpg.Connection) -> None:
    """recent_pins_for_user() orders by created_at. If every save shared a
    timestamp there would be no meaningful "recent" and the feed would be
    arbitrary."""
    span = await conn.fetchrow(
        """
        SELECT count(DISTINCT created_at) AS distinct_ts,
               max(created_at) - min(created_at) AS spread,
               max(created_at) <= now() AS all_in_the_past
        FROM board_pins
        """
    )

    assert span["distinct_ts"] > N_EDGES // 2
    assert span["spread"].days >= EDGE_AGE_DAYS - 1
    assert span["all_in_the_past"] is True


async def test_id_sequences_are_past_the_seeded_rows(conn: asyncpg.Connection) -> None:
    """COPY supplies ids explicitly and bypasses the SERIAL sequences. Without
    the setval() in seed.py, the first API-created row (Phase 3's
    POST /boards/{id}/pins) would reuse id 1 and hit a duplicate key."""
    for table in ("users", "pins", "boards"):
        last_value = await conn.fetchval(
            "SELECT last_value FROM pg_sequences WHERE sequencename = $1",
            f"{table}_id_seq",
        )
        max_id = await conn.fetchval(f"SELECT max(id) FROM {table}")

        assert last_value == max_id, table
