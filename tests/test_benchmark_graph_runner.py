"""Graph-agent runner tests (esim-uzc.4): engines, ontology, reference queries, CLI.

The agent loop needs an API key, so the *keyless* core is what these tests prove:

* the projection turns the gold :class:`~enterprise_sim.core.world.World` into a
  :class:`~enterprise_sim.benchmark.runners.projection.GraphModel` (mentions become
  edges; labels match the sidecar);
* the SPARQL ontology materializes inferred triples
  (``der:reports_to_chain``/``der:in_department``/``der:advances_goal_effective``);
* a hand-written Cypher **and** SPARQL reference query per reasoning type returns
  exactly the gold answer computed straight from the world — proving the engines +
  ontology answer correctly without any agent; and
* the ``bench run`` CLI is wired and reports the missing-key error cleanly.

The keyed agent test runs only when ``ANTHROPIC_API_KEY`` (and ``claude-agent-sdk``)
are present.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.fixtures import golden_run
from enterprise_sim.benchmark.generate import (
    _ancestors,
    _subgoals,
    load_groundings,
)
from enterprise_sim.benchmark.runners.engines import KuzuEngine, OxigraphEngine
from enterprise_sim.benchmark.runners.projection import (
    MENTIONS_EDGE_TYPE,
    GraphModel,
    derive_label,
)
from enterprise_sim.benchmark.runners.reference import REFERENCES_BY_KEY
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.world import World

# --------------------------------------------------------------------------- #
# One gold model, built once for the whole module (the golden run is the slow bit).
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def gold() -> tuple[World, dict[str, list[str]], GraphModel]:
    """The gold world, its answer-key groundings, and the projected graph model."""
    # A module-scoped temp dir would be cleaner, but the world/groundings are fully
    # in memory once built, so a throwaway run dir suffices.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="esim-graph-test-") as tmp:
        result = golden_run(tmp)
        world = result.world
        groundings = load_groundings(result.run_dir, world)
    model = GraphModel.from_world(world, groundings)
    return world, groundings, model


@pytest.fixture(scope="module")
def engines(
    gold: tuple[World, dict[str, list[str]], GraphModel],
) -> Iterator[tuple[KuzuEngine, OxigraphEngine]]:
    """Both embedded engines, built from the gold model."""
    _world, _groundings, model = gold
    kuzu = KuzuEngine.build(model)
    oxigraph = OxigraphEngine.build(model)
    yield kuzu, oxigraph
    kuzu.close()


# --------------------------------------------------------------------------- #
# Projection.
# --------------------------------------------------------------------------- #


def test_derive_label_prefers_props_then_alias_then_id() -> None:
    assert derive_label({"name": "Ada"}, ["Ada Lovelace"], "person:ada") == "Ada"
    assert derive_label({}, ["Ada Lovelace"], "person:ada") == "Ada Lovelace"
    assert derive_label({}, [], "person:ada") == "person:ada"
    # blank/whitespace props are skipped
    assert derive_label({"name": "  "}, ["Ada"], "person:ada") == "Ada"


def test_projection_includes_nodes_edges_and_mention_edges(
    gold: tuple[World, dict[str, list[str]], GraphModel],
) -> None:
    world, groundings, model = gold
    assert len(model.nodes) == world.node_count
    # Every asserted edge is projected, plus one mention edge per (artifact, entity).
    mention_edges = [e for e in model.edges if e.type == MENTIONS_EDGE_TYPE]
    expected_mentions = sum(len(set(arts)) for arts in groundings.values())
    assert len(mention_edges) == expected_mentions
    assert len(model.edges) == world.edge_count + expected_mentions
    # Mention edges run artifact -> entity, both real nodes.
    for edge in mention_edges:
        assert edge.src in world and edge.dst in world


def test_projection_is_deterministic(
    gold: tuple[World, dict[str, list[str]], GraphModel],
) -> None:
    world, groundings, model = gold
    again = GraphModel.from_world(world, groundings)
    assert again == model


# --------------------------------------------------------------------------- #
# Ontology materialization.
# --------------------------------------------------------------------------- #


def test_ontology_materializes_inferred_triples(
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    _kuzu, oxigraph = engines
    assert oxigraph.inferred_count > 0
    # The headline derived predicates exist after materialization.
    for predicate in ("reports_to_chain", "in_department", "advances_goal_effective"):
        result = oxigraph.query(f"SELECT ?s ?o WHERE {{ ?s der:{predicate} ?o }} LIMIT 1")
        assert result.rows, f"expected der:{predicate} triples after materialization"


def test_reports_to_chain_is_transitive(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    """A skip-level chain (>1 manager) proves the closure rule actually closed."""
    world, _groundings, _model = gold
    _kuzu, oxigraph = engines
    person = next(
        p for p in world.nodes_by_type("Person") if len(_ancestors(world, p.id, "reports_to")) > 1
    )
    expected = set(_ancestors(world, person.id, "reports_to"))
    got = set(oxigraph.node_ids(REFERENCES_BY_KEY["management_chain"].sparql(person.id)))
    assert got == expected


# --------------------------------------------------------------------------- #
# Per-reasoning-type reference queries (the keyless "engines proven" check).
# --------------------------------------------------------------------------- #


def _world_reports_to(world: World, pid: str) -> set[str]:
    return {e.dst for e in world.out_edges(pid, "reports_to")}


def _world_department(world: World, pid: str) -> set[str]:
    depts: set[str] = set()
    for membership in world.out_edges(pid, "member_of"):
        team = world.get_node(membership.dst)
        if team is None or team.type != "Team":
            continue
        for part in world.out_edges(team.id, "part_of"):
            dept = world.get_node(part.dst)
            if dept is not None and dept.type == "Department":
                depts.add(dept.id)
    return depts


def _world_team_members(world: World, tid: str) -> set[str]:
    return {e.src for e in world.in_edges(tid, "member_of")}


def _world_goal_advancers(world: World, gid: str) -> set[str]:
    targets = [gid, *_subgoals(world, gid)]
    return {e.src for target in targets for e in world.in_edges(target, "advances_goal")}


def test_reference_query_direct_relation(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    world, _g, _m = gold
    kuzu, oxigraph = engines
    person = next(p for p in world.nodes_by_type("Person") if world.out_edges(p.id, "reports_to"))
    expected = _world_reports_to(world, person.id)
    assert expected
    ref = REFERENCES_BY_KEY["reports_to"]
    assert set(kuzu.node_ids(ref.cypher(person.id))) == expected
    assert set(oxigraph.node_ids(ref.sparql(person.id))) == expected


def test_reference_query_transitive_chain(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    world, _g, _m = gold
    kuzu, oxigraph = engines
    person = next(p for p in world.nodes_by_type("Person") if _ancestors(world, p.id, "reports_to"))
    expected = set(_ancestors(world, person.id, "reports_to"))
    ref = REFERENCES_BY_KEY["management_chain"]
    assert set(kuzu.node_ids(ref.cypher(person.id))) == expected
    assert set(oxigraph.node_ids(ref.sparql(person.id))) == expected


def test_reference_query_transitive_department(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    world, _g, _m = gold
    kuzu, oxigraph = engines
    person = next(p for p in world.nodes_by_type("Person") if _world_department(world, p.id))
    expected = _world_department(world, person.id)
    ref = REFERENCES_BY_KEY["in_department"]
    assert set(kuzu.node_ids(ref.cypher(person.id))) == expected
    assert set(oxigraph.node_ids(ref.sparql(person.id))) == expected


def test_reference_query_aggregation(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    world, _g, _m = gold
    kuzu, oxigraph = engines
    team = next(t for t in world.nodes_by_type("Team") if world.in_edges(t.id, "member_of"))
    expected = _world_team_members(world, team.id)
    assert expected
    ref = REFERENCES_BY_KEY["team_headcount"]
    assert set(kuzu.node_ids(ref.cypher(team.id))) == expected
    assert set(oxigraph.node_ids(ref.sparql(team.id))) == expected


def test_reference_query_goal_tree(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    world, _g, _m = gold
    kuzu, oxigraph = engines
    goal = next(g for g in world.nodes_by_type("Goal") if _world_goal_advancers(world, g.id))
    expected = _world_goal_advancers(world, goal.id)
    ref = REFERENCES_BY_KEY["goal_advancers"]
    assert set(kuzu.node_ids(ref.cypher(goal.id))) == expected
    assert set(oxigraph.node_ids(ref.sparql(goal.id))) == expected


def test_reference_query_provenance(
    gold: tuple[World, dict[str, list[str]], GraphModel],
    engines: tuple[KuzuEngine, OxigraphEngine],
) -> None:
    world, groundings, _m = gold
    kuzu, oxigraph = engines
    entity = next(e for e in sorted(groundings) if groundings[e])
    expected = set(groundings[entity])
    ref = REFERENCES_BY_KEY["provenance"]
    assert set(kuzu.node_ids(ref.cypher(entity))) == expected
    assert set(oxigraph.node_ids(ref.sparql(entity))) == expected


def test_every_reasoning_type_has_a_reference() -> None:
    from enterprise_sim.benchmark.schema import REASONING_TYPES

    covered = {ref.reasoning_type for ref in REFERENCES_BY_KEY.values()}
    assert covered == set(REASONING_TYPES)


# --------------------------------------------------------------------------- #
# Engine ergonomics: search + error surfacing.
# --------------------------------------------------------------------------- #


def test_search_nodes_finds_by_label(
    gold: tuple[World, dict[str, list[str]], GraphModel],
) -> None:
    from enterprise_sim.benchmark.runners.graph_agent import GraphRunner

    _world, _g, model = gold
    runner = GraphRunner(model)
    try:
        person = next(n for n in model.nodes if n.type == "Person")
        matches = runner.search_nodes(person.label)
        assert any(m["id"] == person.id for m in matches)
        assert runner.search_nodes("definitely-not-a-real-entity-xyz") == []
    finally:
        runner.close()


def test_cypher_and_sparql_schema_prompts_mention_engines(
    gold: tuple[World, dict[str, list[str]], GraphModel],
) -> None:
    from enterprise_sim.benchmark.runners.graph_agent import GraphRunner

    _world, _g, model = gold
    runner = GraphRunner(model)
    try:
        prompt = runner.schema_prompt()
        assert "CYPHER" in prompt and "SPARQL" in prompt
        assert "der:reports_to_chain" in prompt
    finally:
        runner.close()


# --------------------------------------------------------------------------- #
# CLI wiring.
# --------------------------------------------------------------------------- #


def test_bench_run_subcommand_registered() -> None:
    args = build_parser().parse_args(
        ["bench", "run", "--runner", "graph", "--bench", "b.jsonl", "-o", "p.jsonl"]
    )
    assert args.runner == "graph"
    assert args.bench == Path("b.jsonl")
    assert args.output == Path("p.jsonl")
    assert args.func is not None


def test_bench_run_requires_api_key(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    """Without a key the graph runner reports cleanly and exits non-zero (gated)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bench_path = tmp_path / "bench.jsonl"
    bench_path.write_text("", encoding="utf-8")
    code = main(["bench", "run", "--runner", "graph", "--bench", str(bench_path)])
    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Keyed agent loop (skipped without a key / the SDK).
# --------------------------------------------------------------------------- #

_HAVE_SDK = importlib.util.find_spec("claude_agent_sdk") is not None


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and _HAVE_SDK),
    reason="needs ANTHROPIC_API_KEY and claude-agent-sdk",
)
def test_graph_agent_produces_scored_predictions(
    gold: tuple[World, dict[str, list[str]], GraphModel],
) -> None:
    """With a key: the agent answers a small subset and the predictions are scorable."""
    from enterprise_sim.benchmark.generate import build_benchmark
    from enterprise_sim.benchmark.runners.graph_agent import run_benchmark
    from enterprise_sim.benchmark.score import score

    world, groundings, _model = gold
    benchmark = build_benchmark(world, groundings)
    predictions = run_benchmark(benchmark, limit=2)
    assert len(predictions) >= 1
    report = score(benchmark, predictions)
    assert report.overall.count == len(benchmark)
