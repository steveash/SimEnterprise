"""KG-QA benchmark: turn the gold knowledge graph into an agent eval.

The sim emits ground truth most eval setups lack — a gold knowledge graph, a
gold answer key (provenance), and a grounded artifact corpus. This package
auto-generates question/answer pairs from that ground truth and scores how well
an agent answers them — from the GRAPH (Cypher/SPARQL reasoning) vs from the
RAW CORPUS (RAG) vs naive baselines (epic esim-uzc).

This module gives the package its schema (:class:`QAPair` and the
:class:`Benchmark` collection, JSONL round-trip), the :mod:`fixtures` helper that
executes the committed golden run and hands generators and tests one
deterministic gold :class:`~enterprise_sim.core.world.World`, and the
:func:`~enterprise_sim.benchmark.generate.generate` Q/A generator that derives
the benchmark from that ground truth (``enterprise-sim bench generate``), and the
:func:`~enterprise_sim.benchmark.score.score` grader with its
:func:`~enterprise_sim.benchmark.score.format_report` rendering
(``enterprise-sim bench score``). The LLM runners and the multi-runner
comparison report are added by later beads. See ``docs/BENCHMARK.md``.
"""

from __future__ import annotations

from enterprise_sim.benchmark.generate import build_benchmark, generate
from enterprise_sim.benchmark.schema import (
    REASONING_TYPES,
    Benchmark,
    QAPair,
)
from enterprise_sim.benchmark.score import (
    Aggregate,
    ItemScore,
    Prediction,
    Predictions,
    Report,
    format_report,
    score,
    score_item,
)

__all__ = [
    "REASONING_TYPES",
    "Aggregate",
    "Benchmark",
    "ItemScore",
    "Prediction",
    "Predictions",
    "QAPair",
    "Report",
    "build_benchmark",
    "format_report",
    "generate",
    "score",
    "score_item",
]
