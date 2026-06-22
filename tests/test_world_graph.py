"""Tests for the in-memory KG store (ARCHITECTURE §11.1, D18).

Covers the acceptance criteria for esim-4440e6e5:

* build a small graph,
* projection-by-timestamp,
* deterministic insertion-order storage + sort-by-id queries/serialization.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from enterprise_sim.core.world import Edge, Event, Node, World, WorldView

# A fixed sim-time origin keeps everything deterministic (no wall-clock).
T0 = datetime(2026, 6, 1, 9, 0, 0)


def _day(n: int) -> datetime:
    return T0 + timedelta(days=n)


def _small_graph() -> World:
    """A tiny org graph: a company, two people, a project, and edges."""
    world = World()
    world.add_node(Node("company:acme", "Company", T0, props={"vertical": "fintech"}))
    world.add_node(
        Node("person:ada", "Person", _day(1), aliases=["Ada", "Ada Lovelace"]),
    )
    world.add_node(Node("person:bob", "Person", _day(1)))
    world.add_node(Node("proj:payments", "Project", _day(2)))
    # Reified edges with their own ids.
    world.add_edge(Edge("edge:member_of:ada", "member_of", "person:ada", "company:acme", _day(1)))
    world.add_edge(Edge("edge:member_of:bob", "member_of", "person:bob", "company:acme", _day(1)))
    world.add_edge(Edge("edge:reports_to:bob", "reports_to", "person:bob", "person:ada", _day(1)))
    world.add_edge(Edge("edge:leads:ada", "leads", "person:ada", "proj:payments", _day(2)))
    return world


# -- build a small graph ----------------------------------------------------


def test_build_small_graph_counts() -> None:
    world = _small_graph()
    assert world.node_count == 4
    assert world.edge_count == 4
    assert "person:ada" in world
    assert "person:nobody" not in world


def test_duplicate_ids_rejected() -> None:
    world = World()
    world.add_node(Node("n1", "Person", T0))
    with pytest.raises(ValueError, match="duplicate node id"):
        world.add_node(Node("n1", "Person", T0))
    world.add_edge(Edge("e1", "knows", "n1", "n1", T0))
    with pytest.raises(ValueError, match="duplicate edge id"):
        world.add_edge(Edge("e1", "knows", "n1", "n1", T0))


def test_node_and_edge_props_and_aliases() -> None:
    world = _small_graph()
    ada = world.get_node("person:ada")
    assert ada is not None
    assert ada.aliases == ["Ada", "Ada Lovelace"]
    assert world.get_node("company:acme").props["vertical"] == "fintech"  # type: ignore[union-attr]
    assert world.get_node("missing") is None
    assert world.get_edge("missing") is None


# -- indexes & traversal ----------------------------------------------------


def test_nodes_by_type_sorted() -> None:
    world = _small_graph()
    people = world.nodes_by_type("Person")
    assert [n.id for n in people] == ["person:ada", "person:bob"]
    assert world.node_types() == ["Company", "Person", "Project"]


def test_edges_by_type_sorted() -> None:
    world = _small_graph()
    members = world.edges_by_type("member_of")
    assert [e.id for e in members] == ["edge:member_of:ada", "edge:member_of:bob"]
    assert world.edge_types() == ["leads", "member_of", "reports_to"]


def test_adjacency_out_and_in_edges() -> None:
    world = _small_graph()
    out = world.out_edges("person:ada")
    assert [e.id for e in out] == ["edge:leads:ada", "edge:member_of:ada"]
    typed = world.out_edges("person:ada", "leads")
    assert [e.id for e in typed] == ["edge:leads:ada"]
    incoming = world.in_edges("company:acme", "member_of")
    assert [e.src for e in incoming] == ["person:ada", "person:bob"]


def test_neighbors_directions() -> None:
    world = _small_graph()
    # ada leads payments and is member_of acme (outgoing).
    out = world.neighbors("person:ada")
    assert [n.id for n in out] == ["company:acme", "proj:payments"]
    # bob reports_to ada (incoming to ada).
    incoming = world.neighbors("person:ada", "reports_to", direction="in")
    assert [n.id for n in incoming] == ["person:bob"]
    both = world.neighbors("person:ada", direction="both")
    assert [n.id for n in both] == ["company:acme", "person:bob", "proj:payments"]


def test_neighbors_skips_dangling_endpoints() -> None:
    world = World()
    world.add_node(Node("a", "Person", T0))
    world.add_edge(Edge("e", "knows", "a", "ghost", T0))  # dst not a node
    assert world.neighbors("a") == []
    # The edge itself is still stored (validator's job to flag dangling refs).
    assert [e.id for e in world.out_edges("a")] == ["e"]


def test_neighbors_invalid_direction() -> None:
    world = _small_graph()
    with pytest.raises(ValueError, match="invalid direction"):
        world.neighbors("person:ada", direction="sideways")  # type: ignore[arg-type]


# -- projection by timestamp ------------------------------------------------


def test_projection_at_timestamp_excludes_future() -> None:
    world = _small_graph()
    # Project at end of day 1: project node (day 2) and leads edge excluded.
    view = world.projection(at=_day(1) + timedelta(hours=12))
    ids = {n.id for n in view.nodes()}
    assert ids == {"company:acme", "person:ada", "person:bob"}
    edge_ids = {e.id for e in view.edges()}
    assert "edge:leads:ada" not in edge_ids
    assert "edge:member_of:ada" in edge_ids


def test_projection_returns_worldview_alias() -> None:
    world = _small_graph()
    view = world.projection(at=_day(10))
    assert isinstance(view, WorldView)
    assert isinstance(view, World)


def test_projection_is_independent_copy() -> None:
    world = _small_graph()
    view = world.projection(at=_day(10))
    view.get_node("company:acme").props["mutated"] = True  # type: ignore[union-attr]
    assert "mutated" not in world.get_node("company:acme").props  # type: ignore[union-attr]


def test_projection_focus_keeps_only_internal_edges() -> None:
    world = _small_graph()
    focus = ["person:ada", "person:bob"]
    view = world.projection(at=_day(10), focus=focus)
    assert {n.id for n in view.nodes()} == {"person:ada", "person:bob"}
    # member_of edges point at acme (out of focus) -> dropped; reports_to kept.
    assert {e.id for e in view.edges()} == {"edge:reports_to:bob"}


def test_projection_window_lower_bound() -> None:
    world = _small_graph()
    # Window of 1 day ending at day 2 -> excludes the company (created at day 0).
    view = world.projection(at=_day(2), window=timedelta(days=1))
    ids = {n.id for n in view.nodes()}
    assert "company:acme" not in ids
    assert "proj:payments" in ids
    assert "person:ada" in ids


def test_projection_filters_events() -> None:
    world = _small_graph()
    world.add_event(Event("ev:early", "MeetingHeld", _day(1), subjects=["person:ada"]))
    world.add_event(Event("ev:late", "MeetingHeld", _day(5), subjects=["proj:payments"]))
    view = world.projection(at=_day(2))
    assert {e.id for e in view.events()} == {"ev:early"}


def test_projection_focus_filters_events_by_reference() -> None:
    world = _small_graph()
    world.add_event(
        Event("ev:ada", "Comment", _day(1), actors={"author": ["person:ada"]}),
    )
    world.add_event(Event("ev:proj", "Update", _day(1), subjects=["proj:payments"]))
    view = world.projection(at=_day(10), focus=["person:ada"])
    assert {e.id for e in view.events()} == {"ev:ada"}


# -- determinism ------------------------------------------------------------


def test_storage_preserves_insertion_order() -> None:
    world = World()
    order = ["n:z", "n:a", "n:m", "n:b"]
    for nid in order:
        world.add_node(Node(nid, "Person", T0))
    assert [n.id for n in world.nodes()] == order


def test_queries_sort_by_id_regardless_of_insertion_order() -> None:
    world = World()
    for nid in ["n:z", "n:a", "n:m"]:
        world.add_node(Node(nid, "Person", T0))
    assert [n.id for n in world.nodes_by_type("Person")] == ["n:a", "n:m", "n:z"]


def test_to_dict_lists_sorted_by_id() -> None:
    world = World()
    for nid in ["n:z", "n:a", "n:m"]:
        world.add_node(Node(nid, "Person", T0))
    data = world.to_dict()
    assert [n["id"] for n in data["nodes"]] == ["n:a", "n:m", "n:z"]


def test_two_builds_produce_identical_json() -> None:
    assert _small_graph().to_json() == _small_graph().to_json()


# -- JSON serialization round-trip ------------------------------------------


def test_json_round_trip() -> None:
    world = _small_graph()
    world.add_event(
        Event(
            "ev:1",
            "DeliverableDrafted",
            _day(3),
            actors={"author": ["person:ada"], "reviewers": ["person:bob"]},
            subjects=["proj:payments"],
            parent_event=None,
            payload={"topic": "weekly status", "tone": "concise"},
        )
    )
    restored = World.from_json(world.to_json())
    assert restored.to_json() == world.to_json()
    ada = restored.get_node("person:ada")
    assert ada is not None
    assert ada.aliases == ["Ada", "Ada Lovelace"]
    assert isinstance(ada.created_at, datetime)
    ev = restored.get_event("ev:1")
    assert ev is not None
    assert ev.actors == {"author": ["person:ada"], "reviewers": ["person:bob"]}
    assert ev.payload["topic"] == "weekly status"


def test_dataclass_to_dict_shapes() -> None:
    node = Node("n", "Person", T0, props={"k": 1}, aliases=["x"])
    assert Node.from_dict(node.to_dict()) == node
    edge = Edge("e", "knows", "a", "b", T0, props={"w": 2})
    assert Edge.from_dict(edge.to_dict()) == edge
    event = Event("ev", "X", T0, actors={"a": ["1"]}, subjects=["s"], payload={"p": 1})
    assert Event.from_dict(event.to_dict()) == event
