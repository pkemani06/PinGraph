"""In-memory bipartite graph of pins and boards, rebuilt at startup from Postgres."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


class BipartiteGraph:
    """Adjacency lists both ways: pin -> boards and board -> pins.

    Both directions are stored because the walk alternates sides, and every hop
    picks a random neighbour -- which a list does in O(1) under randrange.
    """

    def __init__(self) -> None:
        self.pin_to_boards: defaultdict[int, list[int]] = defaultdict(list)
        self.board_to_pins: defaultdict[int, list[int]] = defaultdict(list)

    def add_edge(self, board_id: int, pin_id: int) -> None:
        """Record one save. Not idempotent: a repeat is appended again and doubles
        that neighbour's draw chance, so dedupe stays upstream in Postgres."""
        self.pin_to_boards[pin_id].append(board_id)
        self.board_to_pins[board_id].append(pin_id)

    def load(self, edges: Iterable[tuple[int, int]]) -> None:
        """Build from fetch_all_edges() output. Appends, so call once per graph."""
        for board_id, pin_id in edges:
            self.add_edge(board_id, pin_id)
