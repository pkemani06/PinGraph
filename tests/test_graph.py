"""Tests for app/graph.py on a hand-built graph small enough to reason about.

No database here: the graph takes an edge list, so the structure can be asserted
exactly rather than statistically. The seeded graph is for the eval; these
tests are for the data structure the walk steps through.

The fixture graph, drawn out:

    board 10 -- pin 1
    board 10 -- pin 2      <- pin 2 is on two boards, so it is the pin a walk
    board 11 -- pin 2         from pin 1 can reach in two hops
    board 11 -- pin 3
    board 12 -- pin 4      <- reachable from nothing else: a separate component

Pin 4's component is what the recommender's "disconnected pin never appears"
test will rest on, so it is worth having the structure pinned here first.
"""

from __future__ import annotations

import pytest

from app.graph import BipartiteGraph

EDGES: list[tuple[int, int]] = [(10, 1), (10, 2), (11, 2), (11, 3), (12, 4)]


@pytest.fixture
def graph() -> BipartiteGraph:
    g = BipartiteGraph()
    g.load(EDGES)
    return g


def test_add_edge_records_both_directions() -> None:
    """One save is two adjacency entries. A hop in either direction needs its
    own index; storing one way would make the reverse hop a full scan."""
    g = BipartiteGraph()

    g.add_edge(board_id=10, pin_id=1)

    assert g.pin_to_boards[1] == [10]
    assert g.board_to_pins[10] == [1]


def test_load_builds_every_edge_in_both_directions(graph: BipartiteGraph) -> None:
    assert dict(graph.pin_to_boards) == {1: [10], 2: [10, 11], 3: [11], 4: [12]}
    assert dict(graph.board_to_pins) == {10: [1, 2], 11: [2, 3], 12: [4]}


def test_a_pin_on_several_boards_lists_all_of_them(graph: BipartiteGraph) -> None:
    """The multiplicity the walk converts into a ranking: pin 2 sits on two
    boards, so a walk from pin 1 has two routes to it and one to pin 3."""
    assert graph.pin_to_boards[2] == [10, 11]
    assert graph.pin_to_boards[1] == [10]


def test_two_hops_reach_the_shared_board_neighbours(graph: BipartiteGraph) -> None:
    """Walks the structure by hand -- pin -> boards -> pins -- which is exactly
    one step of the walk, and shows pin 4 is unreachable from pin 1."""
    two_hop = [
        pin
        for board in graph.pin_to_boards[1]
        for pin in graph.board_to_pins[board]
        if pin != 1
    ]

    assert two_hop == [2]
    assert 4 not in two_hop


def test_pin_and_board_ids_do_not_collide(graph: BipartiteGraph) -> None:
    """The real schema has separate SERIAL sequences, so board 10 and pin 10 can
    both exist. Two dicts keep them distinct nodes; one dict keyed by raw id
    would merge them and silently invent edges."""
    assert 10 in graph.board_to_pins
    assert 10 not in graph.pin_to_boards
    assert 1 in graph.pin_to_boards
    assert 1 not in graph.board_to_pins


def test_add_edge_is_not_idempotent(graph: BipartiteGraph) -> None:
    """Documents the invariant rather than endorsing it: nothing in the graph
    dedupes, so the same save applied twice weights that edge double. Phase 5
    must only call add_edge() for edges insert_edge() reported as new."""
    graph.add_edge(board_id=10, pin_id=1)

    assert graph.pin_to_boards[1] == [10, 10]
    assert graph.board_to_pins[10] == [1, 2, 1]


def test_reading_an_unknown_pin_inserts_an_empty_entry(graph: BipartiteGraph) -> None:
    """The cost of defaultdict, and a trap for the recommender.

    Indexing a pin that does not exist returns [] *and* stores it, so a read
    mutates the graph. In Phase 3 that means repeated requests for unknown pin
    ids grow the dict without bound in a long-running replica. related_pins()
    should use .get(pin_id, ()) or an `in` check, not direct indexing.
    """
    assert len(graph.pin_to_boards) == 4

    assert graph.pin_to_boards[999] == []

    assert len(graph.pin_to_boards) == 5
    assert 999 in graph.pin_to_boards


def test_an_empty_graph_has_no_nodes() -> None:
    """load([]) is the startup path on an empty database, and must not raise."""
    g = BipartiteGraph()

    g.load([])

    assert not g.pin_to_boards
    assert not g.board_to_pins
