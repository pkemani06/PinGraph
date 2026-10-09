-- PinGraph schema: users save pins to boards, which forms the bipartite graph.
-- Drop first so reseeding is a single command.

DROP TABLE IF EXISTS board_pins;
DROP TABLE IF EXISTS boards;
DROP TABLE IF EXISTS pins;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id         SERIAL PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE boards (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- topic is the seed-assigned ground truth that scripts/eval.py measures against.
CREATE TABLE pins (
    id         SERIAL PRIMARY KEY,
    title      TEXT NOT NULL,
    topic      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per save: the edges of the bipartite graph.
CREATE TABLE board_pins (
    board_id   INTEGER NOT NULL REFERENCES boards (id) ON DELETE CASCADE,
    pin_id     INTEGER NOT NULL REFERENCES pins (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (board_id, pin_id)
);

-- The PK index is sorted by board_id first, so it only serves board -> pins.
-- This one serves pin -> boards.
CREATE INDEX board_pins_pin_id_idx ON board_pins (pin_id);

-- For recent_pins_for_user(), which walks user -> boards -> board_pins.
CREATE INDEX boards_user_id_idx ON boards (user_id);
