"""Understanding-vs-reasoning attribution over three prediction sets (esim-nc6.8).

The reconstruct epic closes the loop with a three-way comparison on ONE shared
benchmark:

* **oracle** — the graph agent on the GOLD KG. The ceiling: a perfect graph *and*
  the graph reasoner. What's achievable when understanding the corpus is free.
* **reconstructed** — the *same* graph agent on the RECONSTRUCTED KG (nc6.5/6.7).
  Same reasoner, imperfect graph — so the only thing that changed between it and
  the oracle is how faithfully the corpus was understood.
* **rag** — the retrieval baseline (esim-uzc.5): no graph at all, read the answer
  off the raw corpus.

Because oracle and reconstructed share a reasoner, the drop between them isolates
**understanding error** — the cost of having to *build* the graph from text
instead of being handed the gold one. The further drop from reconstructed down to
rag is what the graph *structure* still buys over plain retrieval, even
reconstructed imperfectly. The two gaps sum to the whole advantage the graph
opens over RAG:

    (oracle − rag)  =  (oracle − reconstructed)  +  (reconstructed − rag)
     total graph adv.      understanding gap          reasoning/structure gap

This module scores the three prediction sets against the shared benchmark
(reusing the keyless grader, esim-uzc.3), attaches the reconstruction's fidelity
numbers (nc6.6) as context, and renders the decomposition — overall and per
reasoning type — as markdown. Pure and deterministic: it operates only on
prediction files plus a fidelity summary, with no LLM and no graph engine, so the
same inputs always render the same report. Wired to ``enterprise-sim reconstruct
report``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from enterprise_sim.benchmark.schema import Benchmark
from enterprise_sim.benchmark.score import Aggregate, Predictions, Report, score
from enterprise_sim.reconstruct.fidelity import FidelityReport

__all__ = [
    "ORACLE_NAME",
    "RAG_NAME",
    "RECONSTRUCTED_NAME",
    "Attribution",
    "FidelityContext",
    "Gap",
    "build_attribution",
    "build_report",
    "render_markdown",
]

# The three canonical system roles the report compares, in reading order.
ORACLE_NAME = "oracle"
RECONSTRUCTED_NAME = "reconstructed"
RAG_NAME = "rag"

# An empty aggregate stands in for a reasoning type a system never scored (so a
# missing prediction reads as F1 0.0 rather than raising).
_EMPTY = Aggregate(0, 0.0, 0.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# Fidelity context: just the reconstruction numbers the report displays.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FidelityContext:
    """The reconstruction-fidelity headline numbers shown as report context.

    A thin projection of a :class:`~enterprise_sim.reconstruct.fidelity.FidelityReport`
    (nc6.6) — node/edge F1, entity-resolution error counts, and graph sizes — so
    the attribution report can carry "how good was the reconstructed KG?" alongside
    "how well did reasoning over it do?" without depending on the full report
    object. Built either from a live report or from its serialized JSON dict.
    """

    node_f1: float
    edge_f1: float
    over_merges: int
    under_merges: int
    reconstructed_nodes: int
    gold_nodes: int
    reconstructed_edges: int
    gold_edges: int

    @classmethod
    def from_report(cls, report: FidelityReport) -> FidelityContext:
        """Project a live :class:`FidelityReport` down to the displayed numbers."""
        return cls(
            node_f1=report.nodes.overall.f1,
            edge_f1=report.edges.overall.f1,
            over_merges=report.entity_resolution.over_merges,
            under_merges=report.entity_resolution.under_merges,
            reconstructed_nodes=report.reconstructed_node_count,
            gold_nodes=report.gold_node_count,
            reconstructed_edges=report.reconstructed_edge_count,
            gold_edges=report.gold_edge_count,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FidelityContext:
        """Parse the JSON produced by ``FidelityReport.to_dict`` / ``--json``."""
        sizes = data.get("sizes", {})
        er = data.get("entity_resolution", {})
        return cls(
            node_f1=float(data["nodes"]["overall"]["f1"]),
            edge_f1=float(data["edges"]["overall"]["f1"]),
            over_merges=int(er.get("over_merges", 0)),
            under_merges=int(er.get("under_merges", 0)),
            reconstructed_nodes=int(sizes.get("reconstructed_nodes", 0)),
            gold_nodes=int(sizes.get("gold_nodes", 0)),
            reconstructed_edges=int(sizes.get("reconstructed_edges", 0)),
            gold_edges=int(sizes.get("gold_edges", 0)),
        )


# --------------------------------------------------------------------------- #
# The attribution: three graded systems + the derived gaps.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Gap:
    """The understanding/reasoning decomposition of the graph's advantage.

    All three are macro-F1 differences (positive = the first system leads). By
    construction ``understanding + reasoning == total`` up to float rounding, since
    the ``reconstructed`` term telescopes out of ``oracle − rag``.

    Attributes:
        understanding: ``oracle − reconstructed`` — the cost of imperfect corpus
            understanding, with the reasoner held constant.
        reasoning: ``reconstructed − rag`` — what the (imperfect) graph structure
            still buys over plain retrieval.
        total: ``oracle − rag`` — the full advantage of the oracle over RAG.
    """

    understanding: float
    reasoning: float
    total: float


@dataclass(frozen=True)
class Attribution:
    """Three systems graded on one benchmark, with the derived gaps + fidelity.

    Attributes:
        benchmark_size: The number of questions every system was graded on.
        reasoning_types: The reasoning types present in the benchmark, sorted —
            the rows of every per-type table.
        oracle: The oracle system's graded :class:`Report` (graph agent, gold KG).
        reconstructed: The reconstructed system's :class:`Report` (graph agent,
            reconstructed KG).
        rag: The RAG baseline's :class:`Report`.
        fidelity: The reconstruction's fidelity numbers, or ``None`` when not
            supplied (the context section is then omitted).
    """

    benchmark_size: int
    reasoning_types: tuple[str, ...]
    oracle: Report
    reconstructed: Report
    rag: Report
    fidelity: FidelityContext | None = None

    @property
    def systems(self) -> tuple[tuple[str, Report], ...]:
        """The three graded systems as ``(name, report)`` pairs, in reading order."""
        return (
            (ORACLE_NAME, self.oracle),
            (RECONSTRUCTED_NAME, self.reconstructed),
            (RAG_NAME, self.rag),
        )

    def f1(self, system: Report, reasoning: str | None = None) -> float:
        """Macro-F1 of ``system`` overall (``reasoning=None``) or for one type."""
        if reasoning is None:
            return system.overall.macro_f1
        return system.by_reasoning_type.get(reasoning, _EMPTY).macro_f1

    def gap(self, reasoning: str | None = None) -> Gap:
        """The understanding/reasoning/total :class:`Gap` overall or per type."""
        oracle = self.f1(self.oracle, reasoning)
        reconstructed = self.f1(self.reconstructed, reasoning)
        rag = self.f1(self.rag, reasoning)
        return Gap(
            understanding=oracle - reconstructed,
            reasoning=reconstructed - rag,
            total=oracle - rag,
        )


def build_attribution(
    benchmark: Benchmark,
    *,
    oracle: Predictions,
    reconstructed: Predictions,
    rag: Predictions,
    fidelity: FidelityReport | FidelityContext | None = None,
) -> Attribution:
    """Grade the three prediction sets against ``benchmark`` into an :class:`Attribution`.

    Each of ``oracle`` / ``reconstructed`` / ``rag`` is scored against the shared
    benchmark with the keyless grader (esim-uzc.3). ``fidelity`` may be a live
    :class:`FidelityReport`, an already-projected :class:`FidelityContext`, or
    ``None`` (context omitted); it is normalized to a :class:`FidelityContext`.
    """
    context: FidelityContext | None
    if isinstance(fidelity, FidelityReport):
        context = FidelityContext.from_report(fidelity)
    else:
        context = fidelity

    reasoning_types = tuple(sorted({pair.reasoning_type for pair in benchmark}))
    return Attribution(
        benchmark_size=len(benchmark),
        reasoning_types=reasoning_types,
        oracle=score(benchmark, oracle),
        reconstructed=score(benchmark, reconstructed),
        rag=score(benchmark, rag),
        fidelity=context,
    )


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    """Render a metric to three decimal places (matching the other reports)."""
    return f"{value:.3f}"


def _fmt_signed(value: float) -> str:
    """Render a gap with an explicit sign, so wins/losses read at a glance.

    ``-0.000`` (a tiny negative that rounds to zero) is normalized to ``+0.000``
    so the sign column stays tidy.
    """
    text = f"{value:+.3f}"
    return "+0.000" if text == "-0.000" else text


def _markdown_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A GitHub-flavored markdown table (header + separator + body rows)."""
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _overall_rows(attribution: Attribution) -> list[list[str]]:
    """One row per system for the overall F1/P/R/EM table, best-F1 bolded."""
    best_f1 = max((report.overall.macro_f1 for _, report in attribution.systems), default=0.0)
    rows = []
    for name, report in attribution.systems:
        agg = report.overall
        f1_text = _fmt(agg.macro_f1)
        if agg.macro_f1 == best_f1 and best_f1 > 0.0:
            f1_text = f"**{f1_text}**"
        rows.append(
            [
                name,
                f1_text,
                _fmt(agg.macro_precision),
                _fmt(agg.macro_recall),
                _fmt(agg.exact_match_rate),
                str(agg.count),
            ]
        )
    return rows


def _by_type_rows(attribution: Attribution) -> list[list[str]]:
    """Per-reasoning-type macro-F1, one column per system; the row leader bolded."""
    rows = []
    for reasoning in attribution.reasoning_types:
        per_system = [attribution.f1(report, reasoning) for _, report in attribution.systems]
        best = max(per_system, default=0.0)
        count = next(
            (
                report.by_reasoning_type[reasoning].count
                for _, report in attribution.systems
                if reasoning in report.by_reasoning_type
            ),
            0,
        )
        cells = [reasoning, str(count)]
        for value in per_system:
            text = _fmt(value)
            if value == best and best > 0.0:
                text = f"**{text}**"
            cells.append(text)
        rows.append(cells)
    return rows


def _attribution_rows(attribution: Attribution) -> list[list[str]]:
    """The overall + per-type understanding/reasoning/total gap rows."""

    def row(label: str, count: int, gap: Gap) -> list[str]:
        return [
            label,
            str(count),
            _fmt_signed(gap.understanding),
            _fmt_signed(gap.reasoning),
            _fmt_signed(gap.total),
        ]

    rows = [row("**overall**", attribution.benchmark_size, attribution.gap())]
    for reasoning in attribution.reasoning_types:
        count = next(
            (
                report.by_reasoning_type[reasoning].count
                for _, report in attribution.systems
                if reasoning in report.by_reasoning_type
            ),
            0,
        )
        rows.append(row(reasoning, count, attribution.gap(reasoning)))
    return rows


def _fidelity_lines(context: FidelityContext) -> list[str]:
    """The reconstruction-fidelity context block."""
    return [
        "## Reconstruction fidelity (context)",
        "",
        (
            f"The reconstructed KG scored against the gold graph "
            f"(`reconstruct fidelity`, nc6.6): "
            f"node F1 **{_fmt(context.node_f1)}**, edge F1 **{_fmt(context.edge_f1)}**."
        ),
        "",
        (
            f"- Sizes: {context.reconstructed_nodes} / {context.gold_nodes} nodes, "
            f"{context.reconstructed_edges} / {context.gold_edges} edges "
            f"(reconstructed / gold)."
        ),
        (
            f"- Entity-resolution errors: {context.over_merges} over-merges, "
            f"{context.under_merges} under-merges."
        ),
    ]


def render_markdown(
    attribution: Attribution, *, title: str = "Reconstruct attribution report"
) -> str:
    """Render an :class:`Attribution` as a markdown attribution report.

    Sections: the reconstruction-fidelity context (when supplied), an overall F1
    table across the three systems, a per-``reasoning_type`` F1 breakdown, and the
    understanding-vs-reasoning attribution — each gap signed, overall and per type.
    Pure: the same attribution always renders the same text.
    """
    lines = [
        f"# {title}",
        "",
        (
            f"Benchmark: {attribution.benchmark_size} questions · three systems. "
            f"**oracle** = graph agent on the gold KG (the ceiling); "
            f"**reconstructed** = the same agent on the reconstructed KG; "
            f"**rag** = the corpus-retrieval baseline."
        ),
        "",
    ]

    if attribution.fidelity is not None:
        lines.extend(_fidelity_lines(attribution.fidelity))
        lines.append("")

    lines.extend(["## Overall", ""])
    overall_header = ["System", "F1", "P", "R", "EM", "n"]
    lines.extend(_markdown_table(overall_header, _overall_rows(attribution)))

    lines.extend(["", "## By reasoning type", ""])
    if attribution.reasoning_types:
        lines.append("Macro-F1 per system; **bold** marks the leader in each row.")
        lines.append("")
        by_type_header = ["reasoning_type", "n", ORACLE_NAME, RECONSTRUCTED_NAME, RAG_NAME]
        lines.extend(_markdown_table(by_type_header, _by_type_rows(attribution)))
    else:
        lines.append("_No questions in the benchmark._")

    lines.extend(
        [
            "",
            "## Attribution: understanding vs reasoning",
            "",
            (
                "The oracle's advantage over RAG splits cleanly, because oracle and "
                "reconstructed share the graph reasoner and differ only in graph quality:"
            ),
            "",
            "- **understanding** = oracle − reconstructed: the cost of imperfectly "
            "understanding the corpus (reconstruction error), reasoner held constant.",
            "- **reasoning** = reconstructed − rag: what the graph structure still buys "
            "over plain retrieval, even reconstructed imperfectly.",
            "- **total** = oracle − rag = understanding + reasoning.",
            "",
        ]
    )
    attribution_header = ["reasoning_type", "n", "understanding", "reasoning", "total"]
    lines.extend(_markdown_table(attribution_header, _attribution_rows(attribution)))

    return "\n".join(lines) + "\n"


def build_report(
    benchmark: Benchmark,
    *,
    oracle: Predictions,
    reconstructed: Predictions,
    rag: Predictions,
    fidelity: FidelityReport | FidelityContext | None = None,
    title: str = "Reconstruct attribution report",
) -> str:
    """Build the attribution and render it to markdown in one call."""
    attribution = build_attribution(
        benchmark,
        oracle=oracle,
        reconstructed=reconstructed,
        rag=rag,
        fidelity=fidelity,
    )
    return render_markdown(attribution, title=title)
