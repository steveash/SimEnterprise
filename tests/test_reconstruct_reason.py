"""Reason over the persisted reconstructed KG (esim-nc6.7): wiring + build-once.

The agent loop needs an API key, so — as with the graph runner (esim-uzc.4) — the
*keyless* core is what these tests prove, plus the build-once/answer-many contract:

* a persisted :class:`~enterprise_sim.reconstruct.schema.ReconstructedKG` (nc6.5)
  loads into the SAME projection + engines the gold KG uses, so a reference Cypher
  **and** SPARQL query return the expected node ids — no agent, no key;
* the materialized ontology derives predicates over the reconstruction too
  (``der:in_department`` from ``member_of`` + ``part_of``);
* ``run_benchmark`` builds the engines **once** and reuses that single runner for
  every question (asserted by recording the runner identity per question), and
  leaves a caller-supplied runner open (the caller owns its lifecycle); and
* the ``reconstruct reason`` CLI is wired and reports the missing-key error cleanly.

The keyed agent path is exercised by the graph-runner suite; here the agent loop is
stubbed so the build-once wiring is provable without a key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.runners import graph_agent
from enterprise_sim.benchmark.runners.graph_agent import GraphRunner, run_benchmark
from enterprise_sim.benchmark.runners.projection import GraphModel
from enterprise_sim.benchmark.runners.reference import REFERENCES_BY_KEY
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.world import Edge, Node
from enterprise_sim.reconstruct import ReconstructedKG, project_with_groundings
from enterprise_sim.reconstruct.schema import Provenance

_AT = datetime(1970, 1, 1, tzinfo=UTC)


def _node(node_id: str, node_type: str, name: str) -> Node:
    return Node(id=node_id, type=node_type, created_at=_AT, props={"name": name})


def _edge(rel: str, src: str, dst: str) -> Edge:
    return Edge(id=f"edge:{rel}:{src}:{dst}", type=rel, src=src, dst=dst, created_at=_AT)


def _reconstructed_kg() -> ReconstructedKG:
    """A tiny reconstruction with queryable direct, aggregation, and derived paths.

    alice reports_to boss; alice member_of platform; platform part_of eng — so a
    ``reports_to`` / ``team_headcount`` reference query hits an asserted edge and an
    ``in_department`` one hits the materialized ``der:in_department`` predicate.
    """
    nodes = [
        _node("person:alice", "Person", "Alice"),
        _node("person:boss", "Person", "Boss"),
        _node("team:platform", "Team", "Platform"),
        _node("dept:eng", "Department", "Engineering"),
    ]
    edges = [
        _edge("reports_to", "person:alice", "person:boss"),
        _edge("member_of", "person:alice", "team:platform"),
        _edge("part_of", "team:platform", "dept:eng"),
    ]
    return ReconstructedKG(nodes=nodes, edges=edges)


def _persisted_kg(tmp_path: Path) -> Path:
    """Write the reconstruction to disk and return its dir (the nc6.5 artifact)."""
    out = tmp_path / "reconstructed"
    _reconstructed_kg().write(out)
    return out


def _runner_from(out_dir: Path) -> GraphRunner:
    kg = ReconstructedKG.read(out_dir)
    return GraphRunner(GraphModel.from_world(kg.to_world()))


# --------------------------------------------------------------------------- #
# Keyless: the persisted KG loads into the engines and answers reference queries.
# --------------------------------------------------------------------------- #


def test_reconstructed_kg_loads_into_engines(tmp_path: Path) -> None:
    """A persisted reconstruction loads into both engines (same projection as gold)."""
    out = _persisted_kg(tmp_path)
    runner = _runner_from(out)
    try:
        assert len(runner.model.nodes) == 4
        # Every asserted edge is projected (no groundings ⇒ no mention edges).
        assert len(runner.model.edges) == 3
        assert runner.kuzu.query("MATCH (n) RETURN n.id AS id").rows
        assert runner.oxigraph.size > 0
    finally:
        runner.close()


def test_reference_query_returns_ids_over_reconstruction(tmp_path: Path) -> None:
    """The keyless proof: reference Cypher + SPARQL return the expected ids."""
    out = _persisted_kg(tmp_path)
    runner = _runner_from(out)
    try:
        # Direct relation (asserted edge).
        ref = REFERENCES_BY_KEY["reports_to"]
        assert set(runner.kuzu.node_ids(ref.cypher("person:alice"))) == {"person:boss"}
        assert set(runner.oxigraph.node_ids(ref.sparql("person:alice"))) == {"person:boss"}

        # Aggregation (team membership).
        ref = REFERENCES_BY_KEY["team_headcount"]
        assert set(runner.kuzu.node_ids(ref.cypher("team:platform"))) == {"person:alice"}
        assert set(runner.oxigraph.node_ids(ref.sparql("team:platform"))) == {"person:alice"}

        # Derived predicate materialized over the reconstruction (der:in_department).
        ref = REFERENCES_BY_KEY["in_department"]
        assert set(runner.kuzu.node_ids(ref.cypher("person:alice"))) == {"dept:eng"}
        assert set(runner.oxigraph.node_ids(ref.sparql("person:alice"))) == {"dept:eng"}
    finally:
        runner.close()


def test_provenance_query_answerable_over_reconstruction(tmp_path: Path) -> None:
    """Keyless proof of esim-ecr.2: the provenance reference query returns the
    grounding artifacts when the reconstruction is projected with its groundings.

    ``project_with_groundings`` (the CLI's ``reconstruct reason`` path) turns the
    persisted node provenance into derived ``mentions`` edges, so the provenance
    family — a structural zero without groundings — becomes answerable over the
    reconstructed KG via the same reference Cypher/SPARQL the gold KG uses.
    """
    kg = _reconstructed_kg()
    kg.add_provenance(
        Provenance(target_id="person:alice", source_paths=("docs/plan.md", "docs/spec.md"))
    )
    world, groundings = project_with_groundings(kg)
    runner = GraphRunner(GraphModel.from_world(world, groundings))
    try:
        ref = REFERENCES_BY_KEY["provenance"]
        expected = {"docs/plan.md", "docs/spec.md"}
        assert set(runner.kuzu.node_ids(ref.cypher("person:alice"))) == expected
        assert set(runner.oxigraph.node_ids(ref.sparql("person:alice"))) == expected
    finally:
        runner.close()


# --------------------------------------------------------------------------- #
# Build-once / answer-many: one runner, reused for every question.
# --------------------------------------------------------------------------- #


def _benchmark() -> Benchmark:
    return Benchmark.of(
        QAPair(
            id=f"q{i}",
            question=f"question {i}?",
            qtype="who",
            reasoning_type="direct_relation",
            expected_ids=("person:boss",),
        )
        for i in range(3)
    )


def test_run_benchmark_builds_engines_once(tmp_path: Path, monkeypatch: Any) -> None:
    """Every question is answered against the SAME runner (engines built once)."""
    out = _persisted_kg(tmp_path)
    runner = _runner_from(out)

    seen: list[int] = []

    async def _fake_predict(
        active: GraphRunner, pair: QAPair, *, model: str, max_turns: int
    ) -> list[str]:
        seen.append(id(active))
        return ["person:boss"]

    monkeypatch.setattr(graph_agent, "_predict_pair", _fake_predict)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    benchmark = _benchmark()
    try:
        predictions = run_benchmark(benchmark, runner=runner, model="m")
        # One prediction per question, all produced by the one passed-in runner.
        assert len(predictions) == len(benchmark)
        assert seen == [id(runner)] * len(benchmark)
        # A caller-supplied runner is NOT closed by run_benchmark — still queryable.
        assert runner.kuzu.query("MATCH (n) RETURN n.id AS id").rows
    finally:
        runner.close()


def test_run_benchmark_without_key_raises(tmp_path: Path, monkeypatch: Any) -> None:
    """The agent step is gated even with a pre-built runner (no key ⇒ RuntimeError)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = _persisted_kg(tmp_path)
    runner = _runner_from(out)
    try:
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            run_benchmark(_benchmark(), runner=runner)
    finally:
        runner.close()


# --------------------------------------------------------------------------- #
# CLI wiring.
# --------------------------------------------------------------------------- #


def test_reason_subcommand_registered() -> None:
    args = build_parser().parse_args(
        [
            "reconstruct",
            "reason",
            "--reconstructed",
            "recon",
            "--bench",
            "b.jsonl",
            "-o",
            "p.jsonl",
        ]
    )
    assert args.reconstructed == Path("recon")
    assert args.bench == Path("b.jsonl")
    assert args.output == Path("p.jsonl")
    assert args.func is not None


def test_reason_cli_requires_api_key(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    """Without a key the CLI loads the KG, reports cleanly, and exits non-zero (gated)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = _persisted_kg(tmp_path)
    bench_path = tmp_path / "bench.jsonl"
    _benchmark().write_jsonl(bench_path)

    code = main(
        [
            "reconstruct",
            "reason",
            "--reconstructed",
            str(out),
            "--bench",
            str(bench_path),
        ]
    )
    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
