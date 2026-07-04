"""Model-axis sweep: compare extraction models on reconstruction fidelity (esim-ecr.4).

The threshold sweep (:mod:`~enterprise_sim.reconstruct.sweep`) holds the model
fixed and varies the edge-confidence knob over *one* extraction. This module
varies the orthogonal axis: the **model** that does the extraction (and,
optionally, the reasoning). Extraction quality is the ceiling on reconstruction
fidelity — a stronger model reads more entities and relations correctly out of the
corpus — so the same corpus reconstructed by Haiku vs Sonnet yields different KGs,
different fidelity, and (when a benchmark is supplied) different answer-F1.

Unlike the threshold sweep, the model axis cannot reuse one extraction: each model
must run the gated chunk → extract → resolve prefix itself. So
:func:`sweep_models` runs :func:`~enterprise_sim.reconstruct.build.extract_once`
per model through the *same* backend client (the per-call ``model`` override picks
the model), builds + scores each reconstruction against the gold graph with the
keyless :func:`~enterprise_sim.reconstruct.fidelity.score_fidelity`, and emits a
per-model comparison table as a :class:`ModelSweepReport`.

Answer-F1 is optional and injected: when the caller passes an ``answer_scorer``
(the CLI wires the graph-agent reasoner, which needs a key), each model's built KG
is also reasoned over and graded, so the table carries answer-F1 alongside
fidelity. The harness itself is pure orchestration — the gated work lives in the
client and the scorer — so with the deterministic ``fake`` backend and no scorer it
sweeps a small KG per model and reports keylessly; the ``fake`` backend ignores the
model id, so keyless numbers are identical across models (the label is just
recorded), while real per-model differences are a keyed crew run.

Wired to ``enterprise-sim reconstruct sweep --models``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise_sim.benchmark.score import Report
from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.world import World
from enterprise_sim.reconstruct.build import (
    DEFAULT_BUILD_CONFIG,
    BuildConfig,
    extract_once,
)
from enterprise_sim.reconstruct.fidelity import FidelityReport, score_fidelity
from enterprise_sim.reconstruct.schema import ReconstructedKG

__all__ = [
    "AnswerScorer",
    "ModelPoint",
    "ModelSweepReport",
    "sweep_models",
]


#: An optional answer-F1 hook: reason over a model's reconstructed KG (with that
#: same model) and grade against the benchmark, returning the scored
#: :class:`~enterprise_sim.benchmark.score.Report`. Its callers gate the key — the
#: harness stays keyless when it's absent, and keyless tests inject a fake.
AnswerScorer = Callable[[ReconstructedKG, str], Report]


@dataclass(frozen=True)
class ModelPoint:
    """One row of the sweep: the reconstruction one model produced.

    Attributes:
        model: The model id that ran the gated extraction (and reasoning, when
            scored) — the row's label. Recorded verbatim; the ``fake`` backend
            ignores it, so keyless rows differ only by this label.
        fidelity: Node/edge P/R/F1 of the KG this model reconstructed, scored
            against the gold graph.
        answer: The benchmark grading of reasoning over this model's KG, or
            ``None`` when no ``answer_scorer`` was supplied (fidelity-only sweep).
    """

    model: str
    fidelity: FidelityReport
    answer: Report | None = None

    @property
    def node_f1(self) -> float:
        """Overall node F1 of this model's reconstruction."""
        return self.fidelity.nodes.overall.f1

    @property
    def edge_f1(self) -> float:
        """Overall edge F1 of this model's reconstruction."""
        return self.fidelity.edges.overall.f1

    @property
    def answer_f1(self) -> float | None:
        """Macro answer-F1 over the benchmark, or ``None`` when unscored."""
        return None if self.answer is None else self.answer.overall.macro_f1

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (model, fidelity, optional answer-F1)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "fidelity": self.fidelity.to_dict(),
        }
        if self.answer is not None:
            payload["answer"] = {
                "macro_f1": self.answer.overall.macro_f1,
                "macro_precision": self.answer.overall.macro_precision,
                "macro_recall": self.answer.overall.macro_recall,
                "exact_match_rate": self.answer.overall.exact_match_rate,
                "count": self.answer.overall.count,
            }
        return payload


@dataclass(frozen=True)
class ModelSweepReport:
    """The full model sweep: one :class:`ModelPoint` per model, in sweep order.

    Attributes:
        points: The per-model reconstruction points, in the order the models were
            swept (input order, de-duplicated).
        gold_node_count: Gold node total (recall denominator), constant across models.
        gold_edge_count: Gold edge total (recall denominator), constant across models.
        backend: The reconstruction backend the sweep ran on (``fake`` keyless, or a
            real backend for a keyed run) — recorded so the report is self-describing.
        benchmark_size: Number of questions answer-F1 was graded on, or ``None`` when
            the sweep scored fidelity only.
    """

    points: list[ModelPoint]
    gold_node_count: int
    gold_edge_count: int
    backend: str
    benchmark_size: int | None = None

    @property
    def scored_answers(self) -> bool:
        """Whether this sweep carries answer-F1 (an ``answer_scorer`` was supplied)."""
        return self.benchmark_size is not None

    def best_edge_f1(self) -> ModelPoint | None:
        """The model with the highest edge F1 (ties broken by input order).

        The fidelity headline: which model reconstructs the graph most faithfully.
        ``None`` only when the sweep ran no models.
        """
        if not self.points:
            return None
        # ``max`` keeps the first-seen element on ties, so forward iteration breaks
        # ties toward the earlier input model.
        return max(self.points, key=lambda p: p.edge_f1)

    def best_answer_f1(self) -> ModelPoint | None:
        """The model with the highest answer-F1 (ties broken by input order).

        ``None`` when the sweep ran no models or scored fidelity only.
        """
        scored = [(p, f1) for p in self.points if (f1 := p.answer_f1) is not None]
        if not scored:
            return None
        return max(scored, key=lambda item: item[1])[0]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the whole sweep."""
        best_edge = self.best_edge_f1()
        best_answer = self.best_answer_f1()
        payload: dict[str, Any] = {
            "backend": self.backend,
            "benchmark_size": self.benchmark_size,
            "gold_node_count": self.gold_node_count,
            "gold_edge_count": self.gold_edge_count,
            "best_edge_f1_model": None if best_edge is None else best_edge.model,
            "best_answer_f1_model": None if best_answer is None else best_answer.model,
            "points": [point.to_dict() for point in self.points],
        }
        return payload

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the sweep to a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Render the sweep as a GitHub-flavored markdown document."""
        return render_markdown(self)


def sweep_models(
    run_dir: str,
    gold: World,
    models: Iterable[str],
    client: LLMClient,
    *,
    build_config: BuildConfig = DEFAULT_BUILD_CONFIG,
    answer_scorer: AnswerScorer | None = None,
    backend: str = "fake",
) -> ModelSweepReport:
    """Reconstruct + score ``run_dir``'s corpus once per model and compare them.

    For each (de-duplicated, input-ordered) model, runs the gated chunk → extract →
    resolve prefix (:func:`~enterprise_sim.reconstruct.build.extract_once`) through
    ``client`` with that model, builds the KG under ``build_config``, and scores it
    against ``gold`` with :func:`~enterprise_sim.reconstruct.fidelity.score_fidelity`.
    When ``answer_scorer`` is supplied, the built KG is also reasoned over (with the
    same model) and graded, so each row carries answer-F1. Returns the per-model
    :class:`ModelSweepReport`.

    The harness is pure orchestration: extraction and reasoning are the caller's
    backend/scorer choices, so with the ``fake`` backend and no scorer it runs
    keyless. At least one model is required.
    """
    ordered = list(dict.fromkeys(models))
    if not ordered:
        raise ValueError("sweep_models needs at least one model")

    points: list[ModelPoint] = []
    gold_node_count = 0
    gold_edge_count = 0
    benchmark_size: int | None = None
    for model in ordered:
        extraction = extract_once(run_dir, client, model=model)
        kg = extraction.build(config=build_config)
        fidelity = score_fidelity(kg, gold)
        gold_node_count = fidelity.gold_node_count
        gold_edge_count = fidelity.gold_edge_count
        answer = None if answer_scorer is None else answer_scorer(kg, model)
        if answer is not None:
            benchmark_size = answer.overall.count
        points.append(ModelPoint(model=model, fidelity=fidelity, answer=answer))

    return ModelSweepReport(
        points=points,
        gold_node_count=gold_node_count,
        gold_edge_count=gold_edge_count,
        backend=backend,
        benchmark_size=benchmark_size,
    )


# --------------------------------------------------------------------------- #
# Markdown rendering (CLI).
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    """Render a metric to three decimals (matching the fidelity report)."""
    return f"{value:.3f}"


def _markdown_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """A GitHub-flavored markdown table (header + separator + body rows)."""
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_markdown(report: ModelSweepReport, *, title: str = "Reconstruct model sweep") -> str:
    """Render a :class:`ModelSweepReport` as a markdown document.

    One row per model — node/edge P/R/F1 and reconstructed/gold sizes, plus answer
    P/F1/EM when the sweep scored a benchmark — with callouts for the edge-F1 (and,
    when scored, answer-F1) leader. Pure: the same report always renders the same
    text.
    """
    scored = report.scored_answers
    header = [
        "model",
        "node F1",
        "node P",
        "node R",
        "edge F1",
        "edge P",
        "edge R",
        "recon/gold nodes",
        "recon/gold edges",
    ]
    if scored:
        header.extend(["answer F1", "answer EM"])

    rows: list[list[str]] = []
    for point in report.points:
        fidelity = point.fidelity
        row = [
            point.model,
            _fmt(fidelity.nodes.overall.f1),
            _fmt(fidelity.nodes.overall.precision),
            _fmt(fidelity.nodes.overall.recall),
            _fmt(fidelity.edges.overall.f1),
            _fmt(fidelity.edges.overall.precision),
            _fmt(fidelity.edges.overall.recall),
            f"{fidelity.reconstructed_node_count}/{fidelity.gold_node_count}",
            f"{fidelity.reconstructed_edge_count}/{fidelity.gold_edge_count}",
        ]
        if scored:
            if point.answer is None:
                row.extend(["—", "—"])
            else:
                row.extend(
                    [
                        _fmt(point.answer.overall.macro_f1),
                        _fmt(point.answer.overall.exact_match_rate),
                    ]
                )
        rows.append(row)

    context = (
        f"Gold graph: {report.gold_node_count} nodes, {report.gold_edge_count} edges. "
        f"Each row reconstructs the corpus with a different model (backend: "
        f"{report.backend}) and scores it against the gold graph"
    )
    if scored:
        context += f", then reasons over that KG on {report.benchmark_size} benchmark questions."
    else:
        context += "."

    lines = [
        f"# {title}",
        "",
        context,
        "",
    ]
    lines.extend(_markdown_table(header, rows))

    best_edge = report.best_edge_f1()
    if best_edge is not None:
        lines.extend(
            [
                "",
                (
                    f"**Best edge F1:** {_fmt(best_edge.edge_f1)} by `{best_edge.model}` "
                    f"(node F1={_fmt(best_edge.node_f1)})."
                ),
            ]
        )

    best_answer = report.best_answer_f1()
    if best_answer is not None and best_answer.answer_f1 is not None:
        lines.append(f"**Best answer F1:** {_fmt(best_answer.answer_f1)} by `{best_answer.model}`.")

    return "\n".join(lines) + "\n"
