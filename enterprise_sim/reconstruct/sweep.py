"""Edge-confidence threshold sweep (esim-ecr.3).

The build keeps every candidate edge by default (``edge-threshold=0.0``), which
maximizes recall at the cost of precision — a lot of low-confidence, wrong edges
sneak in. Raising the threshold drops the weakest edges: precision climbs, recall
falls, and somewhere in between sits the F1 sweet spot. This module finds it.

Given one :class:`~enterprise_sim.reconstruct.build.PipelineExtraction` (the
build-once output of chunk → extract → resolve) and the gold
:class:`~enterprise_sim.core.world.World`, :func:`sweep_thresholds` rebuilds the KG
at each threshold — **re-aggregating the *same* extraction, never re-running the
LLM** (:meth:`PipelineExtraction.build`) — and scores each with the keyless
:func:`~enterprise_sim.reconstruct.fidelity.score_fidelity`, emitting a
threshold → node/edge P/R/F1 curve as a :class:`SweepReport`.

Because raising the threshold only *removes* edges, the reconstructed edge set
shrinks monotonically: recall and the kept-edge count are non-increasing across the
sweep, and precision trends upward (dropping low-confidence false positives) toward
the sweet spot before recall collapse pulls F1 back down. Everything here is pure
and deterministic — the extraction is fixed, so the whole curve is a function of the
thresholds — so the harness is exercised keyless on a fake-backend reconstruction.
Wired to ``enterprise-sim reconstruct sweep``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise_sim.core.world import World
from enterprise_sim.reconstruct.build import BuildConfig, PipelineExtraction
from enterprise_sim.reconstruct.fidelity import PRF, score_fidelity

__all__ = [
    "SweepPoint",
    "SweepReport",
    "sweep_thresholds",
]


@dataclass(frozen=True)
class SweepPoint:
    """One row of the sweep: the fidelity of the KG built at ``threshold``.

    Attributes:
        threshold: The edge-confidence threshold this KG was built at (edges with
            aggregated confidence below it are dropped).
        nodes: Overall node P/R/F1 — invariant across the sweep (the threshold gates
            edges only), reported so the curve carries the full node/edge picture.
        edges: Overall edge P/R/F1 — the curve that moves: precision up, recall down
            as the threshold rises.
        reconstructed_node_count: Nodes in the KG built at this threshold.
        reconstructed_edge_count: Edges kept at this threshold (non-increasing as it rises).
    """

    threshold: float
    nodes: PRF
    edges: PRF
    reconstructed_node_count: int
    reconstructed_edge_count: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (threshold, node/edge P/R/F1, sizes)."""
        return {
            "threshold": self.threshold,
            "nodes": self.nodes.to_dict(),
            "edges": self.edges.to_dict(),
            "reconstructed_node_count": self.reconstructed_node_count,
            "reconstructed_edge_count": self.reconstructed_edge_count,
        }


@dataclass(frozen=True)
class SweepReport:
    """The full sweep: one :class:`SweepPoint` per threshold, ordered ascending.

    Attributes:
        points: The per-threshold fidelity points, sorted by ascending threshold.
        gold_node_count: Gold node total (recall denominator for nodes), constant.
        gold_edge_count: Gold edge total (recall denominator for edges), constant.
    """

    points: list[SweepPoint]
    gold_node_count: int
    gold_edge_count: int

    def best_edge_f1(self) -> SweepPoint | None:
        """The point with the highest edge F1 (ties broken by lower threshold).

        The sweep's headline answer — the precision/recall sweet spot for edges.
        ``None`` only when the sweep ran no thresholds.
        """
        if not self.points:
            return None
        return max(self.points, key=lambda p: (p.edges.f1, -p.threshold))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the whole sweep."""
        best = self.best_edge_f1()
        return {
            "gold_node_count": self.gold_node_count,
            "gold_edge_count": self.gold_edge_count,
            "best_edge_f1_threshold": None if best is None else best.threshold,
            "points": [point.to_dict() for point in self.points],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the sweep to a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Render the sweep as a GitHub-flavored markdown document."""
        return render_markdown(self)


def sweep_thresholds(
    extraction: PipelineExtraction,
    gold: World,
    thresholds: Iterable[float],
) -> SweepReport:
    """Build + score the KG at each edge-confidence threshold from one extraction.

    For each (deduplicated, ascending) threshold, re-aggregates ``extraction`` into a
    KG via :meth:`PipelineExtraction.build` — reusing the fixed chunks / extractions /
    resolution, so **no LLM is called per threshold** — and scores it against ``gold``
    with :func:`~enterprise_sim.reconstruct.fidelity.score_fidelity`. Returns the
    node/edge P/R/F1 curve as a :class:`SweepReport`. Pure and deterministic.
    """
    ordered = sorted(dict.fromkeys(float(t) for t in thresholds))
    points: list[SweepPoint] = []
    gold_node_count = 0
    gold_edge_count = 0
    for threshold in ordered:
        kg = extraction.build(config=BuildConfig(edge_confidence_threshold=threshold))
        report = score_fidelity(kg, gold)
        gold_node_count = report.gold_node_count
        gold_edge_count = report.gold_edge_count
        points.append(
            SweepPoint(
                threshold=threshold,
                nodes=report.nodes.overall,
                edges=report.edges.overall,
                reconstructed_node_count=report.reconstructed_node_count,
                reconstructed_edge_count=report.reconstructed_edge_count,
            )
        )
    return SweepReport(
        points=points,
        gold_node_count=gold_node_count,
        gold_edge_count=gold_edge_count,
    )


# --------------------------------------------------------------------------- #
# Markdown rendering (CLI).
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    """Render a metric to three decimal places (matching the fidelity report)."""
    return f"{value:.3f}"


def _markdown_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """A GitHub-flavored markdown table (header + separator + body rows)."""
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_markdown(report: SweepReport, *, title: str = "Reconstruct edge-threshold sweep") -> str:
    """Render a :class:`SweepReport` as a markdown document.

    A single threshold → node/edge P/R/F1 table (one row per threshold, ascending),
    with the kept-edge count, plus a one-line callout of the edge-F1 sweet spot. Pure:
    the same report always renders the same text.
    """
    header = [
        "threshold",
        "node F1",
        "node P",
        "node R",
        "edge F1",
        "edge P",
        "edge R",
        "edges",
    ]
    rows = [
        [
            _fmt(point.threshold),
            _fmt(point.nodes.f1),
            _fmt(point.nodes.precision),
            _fmt(point.nodes.recall),
            _fmt(point.edges.f1),
            _fmt(point.edges.precision),
            _fmt(point.edges.recall),
            str(point.reconstructed_edge_count),
        ]
        for point in report.points
    ]

    lines = [
        f"# {title}",
        "",
        (
            f"Gold graph: {report.gold_node_count} nodes, {report.gold_edge_count} edges. "
            f"Each row rebuilds the KG at that edge-confidence threshold from one shared "
            f"extraction (no re-extraction) and scores it against the gold graph."
        ),
        "",
    ]
    lines.extend(_markdown_table(header, rows))

    best = report.best_edge_f1()
    if best is not None:
        lines.extend(
            [
                "",
                (
                    f"**Best edge F1:** {_fmt(best.edges.f1)} at threshold "
                    f"{_fmt(best.threshold)} "
                    f"(P={_fmt(best.edges.precision)}, R={_fmt(best.edges.recall)}, "
                    f"{best.reconstructed_edge_count} edges kept)."
                ),
            ]
        )

    return "\n".join(lines) + "\n"
