"""Tests for the actor/relationship resolver (ARCHITECTURE §15.3, D28).

Covers the acceptance criteria for esim-81252105:

* **selection determinism** — same seed + inputs reproduce identical picks and
  identical written edges,
* **affinity-reinforcement** — picking reinforces affinity (preferential
  attachment) so go-to collaborators concentrate over a run,
* **exclude / distinct honored** — excluded ids and the anchor never appear, and
  ``distinct`` never yields duplicates.

Plus the supporting mechanics: filter ops, count-range parsing, capacity cap,
inverse-load balancing, expertise match, and edge writing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from enterprise_sim.core.config.seed import substream
from enterprise_sim.core.sim import (
    Filter,
    RankWeights,
    Resolver,
    Selector,
)
from enterprise_sim.core.world import Edge, Node, World

T0 = datetime(2026, 6, 1, 9, 0, 0)


def _day(n: int) -> datetime:
    return T0 + timedelta(days=n)


def _weight(world: World, edge_id: str) -> float:
    """Fetch an existing edge's ``weight`` prop (asserting the edge exists)."""
    edge = world.get_edge(edge_id)
    assert edge is not None, f"missing edge {edge_id!r}"
    return float(edge.props["weight"])


def _org() -> World:
    """A small org: one project plus four engineers with team/expertise/seniority."""
    world = World()
    world.add_node(Node("proj:payments", "Project", T0, props={"team": "payments"}))
    world.add_node(
        Node(
            "person:ada",
            "Person",
            T0,
            props={"team": "payments", "seniority": 3, "expertise": ["payments", "api"]},
        )
    )
    world.add_node(
        Node(
            "person:bob",
            "Person",
            T0,
            props={"team": "payments", "seniority": 2, "expertise": ["payments"]},
        )
    )
    world.add_node(
        Node(
            "person:cat",
            "Person",
            T0,
            props={"team": "payments", "seniority": 1, "expertise": ["frontend"]},
        )
    )
    world.add_node(
        Node(
            "person:dan",
            "Person",
            T0,
            props={"team": "platform", "seniority": 2, "expertise": ["infra"]},
        )
    )
    return world


# -- count-range parsing ----------------------------------------------------


def test_count_range_int() -> None:
    assert Selector("Person", count=2).count_range() == (2, 2)


def test_count_range_string() -> None:
    assert Selector("Person", count="2..3").count_range() == (2, 3)
    assert Selector("Person", count="4").count_range() == (4, 4)


@pytest.mark.parametrize("bad", ["3..1", "-1", "a..b", "1..x"])
def test_count_range_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        Selector("Person", count=bad).count_range()


# -- filter ops -------------------------------------------------------------


def test_filter_ops() -> None:
    node = Node("person:ada", "Person", T0, props={"seniority": 3, "expertise": ["payments"]})
    assert Filter("team", "eq", "payments").matches(node) is False  # missing prop
    assert Filter("seniority", "eq", 3).matches(node)
    assert Filter("seniority", "ne", 2).matches(node)
    assert Filter("seniority", "gte", 3).matches(node)
    assert Filter("seniority", "lte", 2).matches(node) is False
    assert Filter("seniority", "in", [2, 3]).matches(node)
    assert Filter("expertise", "contains", "payments").matches(node)
    assert Filter("expertise", "contains", "infra").matches(node) is False
    assert Filter("type", "eq", "Person").matches(node)


def test_where_filters_candidates() -> None:
    world = _org()
    resolver = Resolver(world)
    sel = Selector(
        "Person",
        where=(Filter("team", "eq", "payments"),),
        count="4",
        rank_by=(),
    )
    res = resolver.resolve(sel, rng=substream(1, "t"), at=_day(1))
    # dan is on the platform team and must be filtered out.
    assert "person:dan" not in res.ids
    assert set(res.ids) == {"person:ada", "person:bob", "person:cat"}


# -- determinism ------------------------------------------------------------


def test_selection_determinism() -> None:
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count="2..3")
    res_a = Resolver(_org()).resolve(sel, rng=substream(42, "review"), at=_day(1))
    res_b = Resolver(_org()).resolve(sel, rng=substream(42, "review"), at=_day(1))
    assert res_a.ids == res_b.ids


def test_written_edges_deterministic() -> None:
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count=2)
    world_a, world_b = _org(), _org()
    res_a = Resolver(world_a).resolve(
        sel, rng=substream(7, "r"), at=_day(1), anchor="proj:payments", relationship="reviews_for"
    )
    res_b = Resolver(world_b).resolve(
        sel, rng=substream(7, "r"), at=_day(1), anchor="proj:payments", relationship="reviews_for"
    )
    assert res_a.relationship_edges == res_b.relationship_edges
    assert res_a.affinity_edges == res_b.affinity_edges
    assert world_a.to_json() == world_b.to_json()


def test_different_seeds_can_differ() -> None:
    # Equal-scored candidates: different seeds should be able to pick differently.
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count=1)
    seen: set[str] = set()
    for s in range(20):
        res = Resolver(_org()).resolve(sel, rng=substream(s, "x"), at=_day(1))
        seen.update(res.ids)
    assert len(seen) > 1


# -- exclude / distinct -----------------------------------------------------


def test_exclude_honored() -> None:
    sel = Selector(
        "Person",
        where=(Filter("team", "eq", "payments"),),
        exclude=("person:ada",),
        count="4",
    )
    res = Resolver(_org()).resolve(sel, rng=substream(1, "e"), at=_day(1))
    assert "person:ada" not in res.ids


def test_anchor_excluded_from_candidates() -> None:
    # An anchor that is itself a Person must never be bound to itself.
    world = _org()
    world.add_node(Node("person:lead", "Person", T0, props={"team": "payments"}))
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count="10")
    res = Resolver(world).resolve(sel, rng=substream(1, "a"), at=_day(1), anchor="person:lead")
    assert "person:lead" not in res.ids


def test_distinct_no_duplicates() -> None:
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count="3", distinct=True)
    res = Resolver(_org()).resolve(sel, rng=substream(3, "d"), at=_day(1))
    assert len(res.ids) == len(set(res.ids))


def test_count_clamped_to_candidate_pool() -> None:
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count="10")
    res = Resolver(_org()).resolve(sel, rng=substream(1, "c"), at=_day(1))
    assert len(res.ids) == 3  # only three payments engineers exist


# -- expertise ranking ------------------------------------------------------


def test_expertise_match_ranks_first() -> None:
    sel = Selector(
        "Person",
        where=(Filter("team", "eq", "payments"),),
        rank_by=("expertise",),
        expertise=("payments",),
        count=1,
    )
    # cat lacks payments expertise; with expertise-only ranking she should not win.
    picks: set[str] = set()
    for s in range(10):
        res = Resolver(_org()).resolve(sel, rng=substream(s, "exp"), at=_day(1))
        picks.update(res.ids)
    assert "person:cat" not in picks
    assert picks <= {"person:ada", "person:bob"}


# -- capacity / inverse load ------------------------------------------------


def test_capacity_cap_drops_overloaded() -> None:
    world = _org()
    # ada already has one active review -> at capacity=1, she is dropped.
    world.add_edge(Edge("edge:reviews_for:ada:old", "reviews_for", "person:ada", "proj:old", T0))
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count="4")
    resolver = Resolver(world, capacity=1, load_edge_types=("reviews_for",))
    res = resolver.resolve(sel, rng=substream(1, "cap"), at=_day(1))
    assert "person:ada" not in res.ids


def test_inverse_load_prefers_idle() -> None:
    world = _org()
    # bob is heavily loaded; with load-dominant weights the idle ada should win.
    for i in range(5):
        world.add_edge(
            Edge(f"edge:reviews_for:bob:{i}", "reviews_for", "person:bob", f"proj:p{i}", T0)
        )
    sel = Selector(
        "Person",
        where=(Filter("id", "in", ["person:ada", "person:bob"]),),
        rank_by=("inverse_load",),
        count=1,
    )
    weights = RankWeights(affinity=0.0, inverse_load=10.0, expertise=0.0, floor=0.01)
    resolver = Resolver(world, weights=weights, load_edge_types=("reviews_for",))
    picks = [resolver.resolve(sel, rng=substream(s, "load"), at=_day(1)).ids[0] for s in range(15)]
    assert picks.count("person:ada") > picks.count("person:bob")


# -- affinity + reinforcement (preferential attachment) ---------------------


def test_seeded_affinity_surfaces_go_to_expert() -> None:
    world = _org()
    # Layer A seeds a strong latent affinity between the project and ada.
    world.add_edge(
        Edge(
            "edge:collaborates_with:person:ada<->proj:payments",
            "collaborates_with",
            "person:ada",
            "proj:payments",
            T0,
            props={"weight": 20.0},
        )
    )
    sel = Selector(
        "Person",
        where=(Filter("team", "eq", "payments"),),
        rank_by=("affinity",),
        count=1,
    )
    weights = RankWeights(affinity=10.0, inverse_load=0.0, expertise=0.0, floor=0.01)
    resolver = Resolver(world, weights=weights)
    picks = [
        resolver.resolve(
            sel, rng=substream(s, "aff"), at=_day(1), anchor="proj:payments", reinforce=False
        ).ids[0]
        for s in range(15)
    ]
    assert picks.count("person:ada") >= 13  # the go-to expert dominates


def test_reinforcement_increments_affinity_weight() -> None:
    world = _org()
    resolver = Resolver(world)
    sel = Selector("Person", where=(Filter("id", "eq", "person:ada"),), count=1)
    edge_id = "edge:collaborates_with:person:ada<->proj:payments"

    res1 = resolver.resolve(
        sel, rng=substream(1, "r1"), at=_day(1), anchor="proj:payments", reinforce=True
    )
    assert res1.affinity_edges == (edge_id,)
    assert _weight(world, edge_id) == 1.0

    resolver.resolve(
        sel, rng=substream(1, "r2"), at=_day(2), anchor="proj:payments", reinforce=True
    )
    # Reinforced, not duplicated.
    assert _weight(world, edge_id) == 2.0
    assert len(world.edges_by_type("collaborates_with")) == 1


def test_preferential_attachment_concentrates_over_run() -> None:
    """Repeated binding self-organises a frequent-collaborator cluster."""
    world = _org()
    weights = RankWeights(affinity=8.0, inverse_load=0.0, expertise=0.0, floor=0.05)
    resolver = Resolver(world, weights=weights)
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count=1)

    counts: dict[str, int] = {}
    for i in range(40):
        res = resolver.resolve(
            sel, rng=substream(99, "run", i), at=_day(i), anchor="proj:payments", reinforce=True
        )
        counts[res.ids[0]] = counts.get(res.ids[0], 0) + 1

    top = max(counts, key=lambda k: counts[k])
    # Preferential attachment: the leader takes the clear majority of binds.
    assert counts[top] > 40 // 2
    # And its affinity edge has accumulated weight beyond the initial pick.
    top_edge = f"edge:collaborates_with:{min(top, 'proj:payments')}<->{max(top, 'proj:payments')}"
    assert _weight(world, top_edge) >= counts[top]


# -- relationship edge writing ----------------------------------------------


def test_relationship_edges_written_into_kg() -> None:
    world = _org()
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count=2)
    res = Resolver(world).resolve(
        sel, rng=substream(5, "w"), at=_day(1), anchor="proj:payments", relationship="reviews_for"
    )
    written = world.edges_by_type("reviews_for")
    assert len(written) == 2
    assert {e.id for e in written} == set(res.relationship_edges)
    for edge in written:
        assert edge.dst == "proj:payments"
        assert edge.src in res.ids
        assert edge.created_at == _day(1)


def test_no_anchor_writes_no_edges() -> None:
    world = _org()
    sel = Selector("Person", where=(Filter("team", "eq", "payments"),), count=2)
    res = Resolver(world).resolve(sel, rng=substream(1, "n"), at=_day(1))
    assert res.relationship_edges == ()
    assert res.affinity_edges == ()
    assert world.edge_count == 0


def test_repeated_relationship_edges_are_distinct_by_time() -> None:
    world = _org()
    resolver = Resolver(world)
    sel = Selector("Person", where=(Filter("id", "eq", "person:ada"),), count=1)
    e1 = resolver.resolve(
        sel, rng=substream(1, "a"), at=_day(1), anchor="proj:payments", relationship="reviews_for"
    ).relationship_edges[0]
    e2 = resolver.resolve(
        sel, rng=substream(1, "b"), at=_day(2), anchor="proj:payments", relationship="reviews_for"
    ).relationship_edges[0]
    assert e1 != e2
    assert len(world.edges_by_type("reviews_for")) == 2
