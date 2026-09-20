"""``evals run``: stream a benchmark runner (rag|graph) over a run's eval set.

(EXPLORER_EVALS.md §2.)

Reuses the existing keyless-gated runners
(:mod:`enterprise_sim.benchmark.runners.rag`,
:mod:`enterprise_sim.benchmark.runners.graph_agent`) but drives them **one
question at a time** (build the engines/index once, answer many) so
:func:`run_eval` can be a generator that yields one ``{"kind": "question", …}``
event per answered question — the streaming shape the sidecar's job-watch
contract expects (``docs/EXPLORER.md`` §3) — ending with a single
``{"kind": "done", "report": …}``. :func:`check_prereqs` is the up-front,
raise-before-yielding-anything gate so a missing key/SDK is a clean error, not
a stream that starts then dies.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from enterprise_sim.benchmark.generate import load_groundings, load_world_from_run
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.benchmark.score import (
    Aggregate,
    ItemScore,
    Predictions,
    Report,
    score,
    score_item,
)
from enterprise_sim.core.world import World

RUNNERS = ("rag", "graph")


class RunnerUnavailable(RuntimeError):
    """Raised when the selected runner's key/SDK prerequisites are missing."""


def check_prereqs(runner: str) -> None:
    """Raise :class:`RunnerUnavailable` if ``runner``'s key/SDK isn't present; else no-op."""
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r}; expected one of {RUNNERS}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RunnerUnavailable(
            f"'{runner}' runner needs ANTHROPIC_API_KEY (it calls the Claude API)"
        )
    if runner == "graph":
        import importlib.util

        if importlib.util.find_spec("claude_agent_sdk") is None:
            raise RunnerUnavailable(
                "'graph' runner needs the claude-agent-sdk package (extras: bench)"
            )


def _aggregate_dict(agg: Aggregate) -> dict[str, Any]:
    return {
        "count": agg.count,
        "exact_match_rate": agg.exact_match_rate,
        "macro_precision": agg.macro_precision,
        "macro_recall": agg.macro_recall,
        "macro_f1": agg.macro_f1,
    }


def _item_dict(item: ItemScore) -> dict[str, Any]:
    return {
        "qa_id": item.qa_id,
        "reasoning_type": item.reasoning_type,
        "expected": list(item.expected),
        "predicted": list(item.predicted),
        "exact_match": item.exact_match,
        "precision": item.precision,
        "recall": item.recall,
        "f1": item.f1,
    }


def report_to_dict(report: Report) -> dict[str, Any]:
    """A JSON-able rendering of a :class:`~enterprise_sim.benchmark.score.Report`."""
    return {
        "overall": _aggregate_dict(report.overall),
        "by_reasoning_type": {k: _aggregate_dict(v) for k, v in report.by_reasoning_type.items()},
        "items": [_item_dict(item) for item in report.items],
    }


def _run_rag(
    run_dir: str | Path,
    pairs: Sequence[QAPair],
    *,
    world: World,
    backend: str,
    top_k: int,
    model: str | None,
) -> Iterator[tuple[QAPair, frozenset[str]]]:
    from enterprise_sim.benchmark.runners.rag import build_runner
    from enterprise_sim.core.llm import LLMConfig, build_client

    rag_runner = build_runner(run_dir, world=world, top_k=top_k)
    client = build_client(LLMConfig(backend=backend))
    for pair in pairs:
        prediction = rag_runner.answer(pair, client, model=model)
        yield pair, frozenset(prediction.predicted_ids)


def _run_graph(
    run_dir: str | Path,
    pairs: Sequence[QAPair],
    *,
    world: World,
    model: str | None,
) -> Iterator[tuple[QAPair, frozenset[str]]]:
    from enterprise_sim.benchmark.runners.graph_agent import (
        DEFAULT_MODEL,
        GraphRunner,
        run_benchmark,
    )

    groundings = load_groundings(run_dir, world)
    graph_runner = GraphRunner.from_world(world, groundings)
    try:
        for pair in pairs:
            predictions = run_benchmark(
                Benchmark.of([pair]), runner=graph_runner, model=model or DEFAULT_MODEL
            )
            yield pair, predictions.ids_for(pair.id)
    finally:
        graph_runner.close()


def run_eval(
    run_dir: str | Path,
    pairs: Sequence[QAPair],
    *,
    runner: str,
    model: str | None = None,
    backend: str = "anthropic_api",
    top_k: int = 5,
) -> Iterator[dict[str, Any]]:
    """Answer ``pairs`` with ``runner``, yielding one ``question`` event per answer.

    The final event is ``{"kind": "done", "report": …}`` (:func:`report_to_dict`).
    Raises :class:`RunnerUnavailable` up front — before yielding anything — if
    the runner's prerequisites are missing, so a caller streaming this either
    gets a clean error or a complete stream, never a stream that starts then
    fails partway.
    """
    check_prereqs(runner)
    world = load_world_from_run(run_dir)
    stream = (
        _run_graph(run_dir, pairs, world=world, model=model)
        if runner == "graph"
        else _run_rag(run_dir, pairs, world=world, backend=backend, top_k=top_k, model=model)
    )

    rows: dict[str, tuple[str, ...]] = {}
    for pair, predicted_ids in stream:
        predicted = tuple(sorted(predicted_ids))
        rows[pair.id] = predicted
        item = score_item(pair, frozenset(predicted))
        yield {
            "kind": "question",
            "id": pair.id,
            "predicted_ids": list(predicted),
            "expected_ids": list(pair.expected_ids),
            "precision": item.precision,
            "recall": item.recall,
            "f1": item.f1,
            "exact": item.exact_match,
        }

    predictions = Predictions.from_mapping(rows)
    report = score(Benchmark.of(pairs), predictions)
    yield {"kind": "done", "report": report_to_dict(report)}


__all__ = ["RUNNERS", "RunnerUnavailable", "check_prereqs", "report_to_dict", "run_eval"]
