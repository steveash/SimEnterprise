"""Benchmark runners: answer the KG-QA benchmark from different sources (epic esim-uzc).

A *runner* takes the gold benchmark and produces a
:class:`~enterprise_sim.benchmark.score.Predictions` set — one predicted node-id
answer per question — so the grader (esim-uzc.3) can score it. Different runners
answer from different sources and are the comparison the epic is about:

* the **RAG** baseline (:mod:`~enterprise_sim.benchmark.runners.rag`, esim-uzc.5)
  answers from the RAW artifact corpus (retrieval + LLM + id resolution); and
* the **graph** agent (:mod:`~enterprise_sim.benchmark.runners.graph_agent`,
  esim-uzc.4) reasons over the gold knowledge graph through two embedded engines —
  :class:`~enterprise_sim.benchmark.runners.engines.KuzuEngine` (Cypher) and
  :class:`~enterprise_sim.benchmark.runners.engines.OxigraphEngine` (SPARQL, with
  the ontology/inference rules ported from the graph-explorer sidecar).

The graph engine + ontology layer (:mod:`~enterprise_sim.benchmark.runners.projection`,
:mod:`~enterprise_sim.benchmark.runners.engines`,
:mod:`~enterprise_sim.benchmark.runners.reference`) is fully usable and testable
without an API key; only the agent loop in
:mod:`~enterprise_sim.benchmark.runners.graph_agent` needs one.
"""

from __future__ import annotations

from enterprise_sim.benchmark.runners.engines import (
    INFERENCE_RULES,
    KuzuEngine,
    OxigraphEngine,
    SparqlResult,
)
from enterprise_sim.benchmark.runners.projection import GraphModel, ModelEdge, ModelNode
from enterprise_sim.benchmark.runners.rag import (
    AliasResolver,
    BM25Index,
    Chunk,
    RagRunner,
    build_runner,
    extract_text,
    load_corpus,
    run_rag,
)

__all__ = [
    "INFERENCE_RULES",
    "AliasResolver",
    "BM25Index",
    "Chunk",
    "GraphModel",
    "KuzuEngine",
    "ModelEdge",
    "ModelNode",
    "OxigraphEngine",
    "RagRunner",
    "SparqlResult",
    "build_runner",
    "extract_text",
    "load_corpus",
    "run_rag",
]
