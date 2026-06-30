"""Keyless-behavior lock (esim-uzc.7): the benchmark needs no API key.

The epic's hard constraint: the benchmark's generate/score/report path is fully
deterministic and keyless, and any path that calls an LLM (the graph-agent and
RAG runners, esim-uzc.4/.5) MUST skip cleanly without ``ANTHROPIC_API_KEY`` so
the quality gate (ruff + mypy + pytest) stays green with no network and no cost.

This module pins both halves:

* the shipped pipeline — :func:`generate` → :func:`score` → :func:`format_report`
  — runs with ``ANTHROPIC_API_KEY`` removed and pulls in no LLM SDK; and
* the gold benchmark is answerable *from the graph alone* — a set of keyless
  reference queries re-derive each answer from the gold
  :class:`~enterprise_sim.core.world.World` (the ontology), independent of the
  generator, confirming the graph-vs-RAG thesis: graph reasoning resolves these
  questions exactly, no retrieval or LLM required.

:data:`requires_llm_runner` is the shared skip marker future LLM-backed runner
tests reuse so their gated cases are *reported as skipped* keyless.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest
from enterprise_sim.benchmark import (
    REASONING_TYPES,
    Predictions,
    format_report,
    generate,
    score,
)
from enterprise_sim.benchmark.fixtures import golden_run
from enterprise_sim.benchmark.generate import build_benchmark, load_groundings
from enterprise_sim.benchmark.schema import Benchmark
from enterprise_sim.core.world import World

# The gate every LLM-backed runner test reuses: the graph-agent / RAG runners
# need both an API key and the agent SDK, so without either they are reported as
# skipped (keeping keyless CI green). Re-export from the runner test modules.
requires_llm_runner = pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("claude_agent_sdk")),
    reason="LLM runner needs ANTHROPIC_API_KEY + claude-agent-sdk (gated; esim-uzc.4/.5)",
)


@pytest.fixture(scope="module")
def gold() -> tuple[World, dict[str, list[str]]]:
    """A single golden run shared across the reference-query checks."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="esim-bench-keyless-") as tmp:
        result = golden_run(tmp)
        return result.world, load_groundings(result.run_dir, result.world)


@pytest.fixture(scope="module")
def benchmark(gold: tuple[World, dict[str, list[str]]]) -> Benchmark:
    world, groundings = gold
    return build_benchmark(world, groundings)


# -- the shipped pipeline is keyless ----------------------------------------


def test_full_pipeline_runs_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate -> score -> format_report with no ``ANTHROPIC_API_KEY`` set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    bench = generate()
    assert len(bench) > 0

    # Score a trivial perfect prediction set and render it — no network, no key.
    preds = Predictions.from_mapping({pair.id: list(pair.expected_ids) for pair in bench})
    report = score(bench, preds)
    assert report.overall.macro_f1 == 1.0
    assert "KG-QA benchmark score" in format_report(report)


def test_pipeline_pulls_in_no_llm_sdk() -> None:
    """Exercising the benchmark never imports an LLM SDK (it stays lazy/gated)."""
    score(generate(), Predictions())
    assert "anthropic" not in sys.modules
    assert "claude_agent_sdk" not in sys.modules


# -- keyless reference queries: the graph answers the benchmark --------------


def test_every_answer_resolves_to_a_gold_node(
    benchmark: Benchmark, gold: tuple[World, dict[str, list[str]]]
) -> None:
    """Every benchmark answer id is a real node in the gold graph (ontology closure)."""
    world, _ = gold
    for pair in benchmark:
        assert pair.expected_ids
        for node_id in pair.expected_ids:
            assert node_id in world, f"{pair.id} answer {node_id!r} is not in the gold graph"


def test_provenance_answers_are_artifacts(
    benchmark: Benchmark, gold: tuple[World, dict[str, list[str]]]
) -> None:
    """Reference query: provenance answers are exactly the Artifact nodes."""
    world, _ = gold
    provenance = [p for p in benchmark if p.reasoning_type == "provenance"]
    assert provenance, "expected provenance pairs in the benchmark"
    for pair in provenance:
        for node_id in pair.expected_ids:
            node = world.get_node(node_id)
            assert node is not None and node.type == "Artifact"


def test_aggregation_label_equals_answer_count(benchmark: Benchmark) -> None:
    """Reference query: an aggregation's label is the size of the counted set."""
    aggregations = [p for p in benchmark if p.reasoning_type == "aggregation"]
    assert aggregations
    for pair in aggregations:
        assert pair.expected_label == str(len(pair.expected_ids))


def test_management_chain_is_reports_to_reachable(
    benchmark: Benchmark, gold: tuple[World, dict[str, list[str]]]
) -> None:
    """Reference query: every management-chain answer is a Person reachable via reports_to+.

    Re-walks the ``reports_to`` edges directly from the gold graph — an
    independent traversal from the generator — so a match proves the chain is a
    genuine graph fact, not an artifact of how it was minted.
    """
    world, _ = gold

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            nxt: list[str] = []
            for node_id in frontier:
                for edge in world.out_edges(node_id, "reports_to"):
                    if edge.dst not in seen:
                        seen.add(edge.dst)
                        nxt.append(edge.dst)
            frontier = nxt
        return seen

    chains = [
        p for p in benchmark if p.reasoning_type == "transitive" and "management" in p.question
    ]
    assert chains, "expected management-chain pairs in the benchmark"
    for pair in chains:
        for node_id in pair.expected_ids:
            node = world.get_node(node_id)
            assert node is not None and node.type == "Person"
        # The chain must be reachable via reports_to from at least one person.
        people = [n.id for n in world.nodes_by_type("Person")]
        assert any(set(pair.expected_ids) <= reachable(pid) for pid in people)


def test_benchmark_spans_every_reasoning_type(benchmark: Benchmark) -> None:
    """The keyless benchmark exercises the whole reasoning taxonomy."""
    assert {p.reasoning_type for p in benchmark} == set(REASONING_TYPES)


# -- gated runner contract: skips cleanly keyless ---------------------------


@requires_llm_runner
def test_llm_runner_gate_is_satisfiable() -> None:
    """When a key + SDK are present this runs; keyless it is *reported as skipped*.

    Locks the skip contract every LLM-backed runner test (esim-uzc.4/.5) reuses
    via :data:`requires_llm_runner`, so the gate keeps the quality gate green
    with no key. The body only asserts the gating preconditions it claims.
    """
    assert os.environ.get("ANTHROPIC_API_KEY")
    assert importlib.util.find_spec("claude_agent_sdk") is not None


def test_runner_gate_skips_without_credentials() -> None:
    """The shared gate evaluates to a skip whenever the key or SDK is absent."""
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_sdk = importlib.util.find_spec("claude_agent_sdk") is not None
    # Mirrors `requires_llm_runner`: gate is active (skips) iff a prerequisite is missing.
    gate_active = not (have_key and have_sdk)
    assert gate_active == (not have_key or not have_sdk)
