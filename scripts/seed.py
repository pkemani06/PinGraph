"""Seed Postgres with synthetic pins, boards, and saves.

The point of this script is not just to fill tables. It builds *topic clusters*,
which are the ground truth that scripts/eval.py measures the recommender against.

Every pin gets exactly one topic. Every board gets one "home" topic and draws
most of its pins from that topic, the rest from anywhere. So two pins that share
a board are usually (not always) on the same subject. A random walk that follows
pin -> board -> pin should therefore surface same-topic pins, and precision@10
tells us how well it does. Without the clusters there is nothing to score
against and we would be eyeballing recommendations.

Why "most" and not "all": if boards never mixed topics, the graph would be 20
disconnected islands. Every algorithm would then score ~100% precision -- a
correct walk, a broken walk, and uniform random sampling from the island would
all tie, and the metric would stop being evidence of anything. The cross-topic
saves are what make the graph one connected component with a signal that has to
actually be found. The number to beat is the ~5% random baseline (1/20 topics),
not 100%.

Usage: python scripts/seed.py [--in-topic-frac 0.8]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

# This runs as a script (python scripts/seed.py), so sys.path[0] is scripts/ and
# the app package is not importable. Put the repo root on the path so we can
# reuse app.config instead of re-deriving the DSN from env vars here.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402  (must follow the sys.path insert)

SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"

# --- Tunables -----------------------------------------------------------------
# Named constants rather than magic numbers so the data shape is auditable, and
# so Phase 2 can reseed with a different mix and confirm precision@10 moves with
# it. A metric that responds correctly to a known change in the data is much
# stronger evidence than a single number.

TOPICS: list[str] = [
    "home-decor",
    "recipes",
    "travel",
    "fashion",
    "fitness",
    "gardening",
    "diy-crafts",
    "weddings",
    "photography",
    "art",
    "tattoos",
    "hairstyles",
    "woodworking",
    "cars",
    "pets",
    "baking",
    "camping",
    "interior-design",
    "nail-art",
    "quotes",
]

N_USERS = 1_000
BOARDS_PER_USER = 5  # -> 5,000 boards
PINS_PER_TOPIC = 500  # -> 10,000 pins across 20 topics
PINS_PER_BOARD = 40  # -> 5,000 * 40 = 200,000 edges
IN_TOPIC_FRACTION = 0.8  # share of a board's pins taken from its home topic
EDGE_AGE_DAYS = 90  # saves are spread over this window, see build_edges
RANDOM_SEED = 42  # fixed so reseeds are reproducible and evals comparable

N_PINS = PINS_PER_TOPIC * len(TOPICS)
N_BOARDS = N_USERS * BOARDS_PER_USER


# --- Row generation -----------------------------------------------------------
# Everything is built in memory first, then bulk loaded. Generating is cheap;
# the database round trips are the expensive part, so we batch them.


def build_users() -> list[tuple[int, str]]:
    """One row per user. Users exist only to own boards (boards.user_id is NOT NULL).

    created_at is left out of the tuple -- COPY applies the column default now()
    for any column not in the column list, so there is no reason to generate it.
    """
    return [(user_id, f"user{user_id}") for user_id in range(1, N_USERS + 1)]


def build_pins() -> tuple[list[tuple[int, str, str]], list[str], dict[str, list[int]]]:
    """One row per pin, each assigned exactly one topic.

    Topics are assigned round-robin over the id space (pin 1 -> topic 0, pin 2 ->
    topic 1, ...) rather than in contiguous blocks. That matters for eval
    integrity: with contiguous blocks, pins of the same topic would have adjacent
    ids, so a bug that returned numerically nearby pin ids would score high
    precision for the wrong reason. Interleaving removes that coincidence.

    Returns the rows to load, a pin_id -> topic lookup, and a topic -> pin_ids
    index. The last two are what build_edges needs to form clusters.
    """
    rows: list[tuple[int, str, str]] = []
    # Index 0 is unused so the list can be indexed directly by pin id.
    pin_topics: list[str] = [""] * (N_PINS + 1)
    pins_by_topic: dict[str, list[int]] = {topic: [] for topic in TOPICS}

    for pin_id in range(1, N_PINS + 1):
        topic = TOPICS[(pin_id - 1) % len(TOPICS)]
        # Per-topic ordinal, so titles read "recipes pin 1", "recipes pin 2", ...
        ordinal = len(pins_by_topic[topic]) + 1
        rows.append((pin_id, f"{topic} pin {ordinal}", topic))
        pin_topics[pin_id] = topic
        pins_by_topic[topic].append(pin_id)

    return rows, pin_topics, pins_by_topic


def build_boards(rng: random.Random) -> tuple[list[tuple[int, int, str]], list[str]]:
    """One row per board, each with a randomly chosen home topic.

    Home topics are drawn at random rather than assigned round-robin so topic
    sizes vary a little (~250 boards each on average) instead of being perfectly
    uniform. Uniform-by-construction data can hide bugs that only show up on
    uneven inputs.

    The home topic is deliberately NOT stored in the boards table -- PLAN.md
    specifies boards as (user_id, title) only, and the recommender must not be
    able to read a topic label off a board. It is returned here so build_edges
    can use it, and so the script can report the mix it actually produced.
    """
    rows: list[tuple[int, int, str]] = []
    board_topics: list[str] = [""] * (N_BOARDS + 1)  # index 0 unused

    for board_id in range(1, N_BOARDS + 1):
        # Boards are handed out in blocks, so each user owns BOARDS_PER_USER.
        user_id = (board_id - 1) // BOARDS_PER_USER + 1
        topic = rng.choice(TOPICS)
        rows.append((board_id, user_id, f"{topic} board {board_id}"))
        board_topics[board_id] = topic

    return rows, board_topics


def build_edges(
    rng: random.Random,
    board_topics: list[str],
    pin_topics: list[str],
    pins_by_topic: dict[str, list[int]],
    in_topic_frac: float,
) -> tuple[list[tuple[int, int, datetime]], int]:
    """One row per save. These are the edges of the bipartite graph.

    For each board: take `n_in_topic` pins from its home topic, then top up to
    PINS_PER_BOARD with pins drawn from the whole catalogue. The top-up draws
    from everything rather than from "other topics only", so roughly 1 in 20 of
    them lands back in the home topic by chance -- the realised in-topic share
    comes out slightly above in_topic_frac. The script reports the measured
    share so the number is observed, not assumed.

    Returns the edge rows and the count that turned out to be in-topic.
    """
    n_in_topic = round(PINS_PER_BOARD * in_topic_frac)
    now = datetime.now(timezone.utc)
    window_seconds = EDGE_AGE_DAYS * 24 * 60 * 60

    edges: list[tuple[int, int, datetime]] = []
    in_topic_edges = 0

    for board_id in range(1, N_BOARDS + 1):
        home_topic = board_topics[board_id]

        # sample() draws without replacement, so a board can never pick the same
        # pin twice -- which would violate the (board_id, pin_id) primary key.
        chosen: set[int] = set(rng.sample(pins_by_topic[home_topic], n_in_topic))

        # Top up with pins from anywhere. Adding to a set and looping until it
        # reaches the target size handles collisions for free, and guarantees
        # every board ends up with exactly PINS_PER_BOARD pins (so the total edge
        # count is exactly N_BOARDS * PINS_PER_BOARD, not slightly under it).
        while len(chosen) < PINS_PER_BOARD:
            chosen.add(rng.randrange(1, N_PINS + 1))

        for pin_id in chosen:
            # Stagger created_at across the window instead of letting it default
            # to now(). recent_pins_for_user() orders by this column, so if every
            # save shared a timestamp there would be no meaningful "recent" and
            # the Phase 3 feed endpoint would return arbitrary pins.
            saved_at = now - timedelta(seconds=rng.randrange(window_seconds))
            edges.append((board_id, pin_id, saved_at))

            if pin_topics[pin_id] == home_topic:
                in_topic_edges += 1

    return edges, in_topic_edges


# --- Database -----------------------------------------------------------------


async def apply_schema(conn: asyncpg.Connection) -> None:
    """Recreate the tables from db/schema.sql.

    schema.sql starts with DROP TABLE IF EXISTS, so this makes the whole script
    idempotent: `python scripts/seed.py` is one command that always leaves the
    database in a known state, rather than appending to whatever was there.
    """
    await conn.execute(SCHEMA_PATH.read_text())


async def load_tables(
    conn: asyncpg.Connection,
    users: list[tuple[int, str]],
    pins: list[tuple[int, str, str]],
    boards: list[tuple[int, int, str]],
    edges: list[tuple[int, int, datetime]],
) -> None:
    """Bulk load every table with COPY.

    copy_records_to_table maps to Postgres COPY, which streams all rows in one
    statement. 200k individual INSERTs would mean 200k round trips and take
    minutes; COPY keeps the whole seed inside the one-minute target. Order
    matters because of the foreign keys: users before boards, pins before edges.
    """
    await conn.copy_records_to_table("users", records=users, columns=["id", "username"])
    await conn.copy_records_to_table(
        "pins", records=pins, columns=["id", "title", "topic"]
    )
    await conn.copy_records_to_table(
        "boards", records=boards, columns=["id", "user_id", "title"]
    )
    await conn.copy_records_to_table(
        "board_pins", records=edges, columns=["board_id", "pin_id", "created_at"]
    )

    # COPY supplies ids explicitly, which bypasses the SERIAL sequences and
    # leaves them at 1. Without this, the first API-created row (Phase 3's
    # POST /boards/{id}/pins) would try to reuse id 1 and hit a duplicate key.
    for table in ("users", "pins", "boards"):
        await conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
            f" (SELECT max(id) FROM {table}))"
        )


async def report_counts(conn: asyncpg.Connection) -> asyncpg.Record:
    """Read the row counts back out of the database.

    Counted in SQL rather than taken from len() on the generated lists, so the
    acceptance check reflects what Postgres actually stored.
    """
    return await conn.fetchrow(
        """
        SELECT (SELECT count(*) FROM users)      AS users,
               (SELECT count(*) FROM pins)       AS pins,
               (SELECT count(*) FROM boards)     AS boards,
               (SELECT count(*) FROM board_pins) AS board_pins
        """
    )


# --- Entry point --------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed Postgres with synthetic data.")
    parser.add_argument(
        "--in-topic-frac",
        type=float,
        default=IN_TOPIC_FRACTION,
        help=(
            "share of each board's pins drawn from its home topic "
            f"(default {IN_TOPIC_FRACTION}). Lower it to make the graph noisier "
            "and precision@10 harder to achieve."
        ),
    )
    args = parser.parse_args()
    if not 0.0 <= args.in_topic_frac <= 1.0:
        parser.error("--in-topic-frac must be between 0.0 and 1.0")
    return args


async def main() -> None:
    args = parse_args()
    # A seeded Random instance, not the module-level random functions, so this
    # script's output does not depend on anything else touching global state.
    rng = random.Random(RANDOM_SEED)
    started = time.perf_counter()

    print(f"generating rows (in-topic fraction {args.in_topic_frac})...")
    users = build_users()
    pins, pin_topics, pins_by_topic = build_pins()
    boards, board_topics = build_boards(rng)
    edges, in_topic_edges = build_edges(
        rng, board_topics, pin_topics, pins_by_topic, args.in_topic_frac
    )

    conn = await asyncpg.connect(settings.postgres_dsn)
    try:
        print("applying db/schema.sql...")
        await apply_schema(conn)
        print("loading tables...")
        await load_tables(conn, users, pins, boards, edges)
        counts = await report_counts(conn)
    finally:
        await conn.close()

    elapsed = time.perf_counter() - started
    print(
        f"\nusers={counts['users']} pins={counts['pins']} "
        f"boards={counts['boards']} board_pins={counts['board_pins']}"
    )
    # The realised mix, measured during generation. The clusters are the whole
    # reason this data exists, so it is worth seeing rather than trusting.
    print(f"in-topic edges: {in_topic_edges / len(edges):.1%} of {len(edges)}")
    print(f"done in {elapsed:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
