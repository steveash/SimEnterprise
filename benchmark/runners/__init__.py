"""Benchmark runners: answer the KG-QA benchmark from different sources (epic esim-uzc).

A *runner* takes the gold benchmark and produces a
:class:`~enterprise_sim.benchmark.score.Predictions` set — one predicted node-id
answer per question — so the grader (esim-uzc.3) can score it. Different runners
answer from different sources and are the comparison the epic is about: the GRAPH
runner reasons over the gold knowledge graph, while the RAG runner here answers
the same questions from the RAW artifact corpus (retrieval + LLM + id resolution).

This module re-exports the RAG baseline (:mod:`~enterprise_sim.benchmark.runners.rag`);
later beads add the graph runner and the naive baselines alongside it.
"""

from __future__ import annotations

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
    "AliasResolver",
    "BM25Index",
    "Chunk",
    "RagRunner",
    "build_runner",
    "extract_text",
    "load_corpus",
    "run_rag",
]
