"""Compare benchmark runners side by side (esim-uzc.6).

The scorer (esim-uzc.3) grades *one* runner's predictions into a
:class:`~enterprise_sim.benchmark.score.Report`. This module stacks several
runners — the graph agent, the RAG baseline, any heuristic — against the same
benchmark and synthesizes a **leaderboard**: a markdown report with overall
macro-F1 per runner and a per-``reasoning_type`` breakdown, so it's visible at a
glance who wins where (graph on ``transitive``/``goal_tree``, RAG competitive on
``provenance``, etc.).

Every report includes a trivial **baseline** runner derived from the benchmark
itself — :func:`most_frequent_baseline`, which ignores the question and always
guesses the single most common gold node id. It establishes the floor a real
runner must clear; if a runner can't beat "always say the most common answer,"
the benchmark is telling you something.

Like the scorer this is pure and keyless: it operates only on prediction files
(no LLM, no graph engine), so the same inputs always render the same markdown.
Entry point: :func:`build_leaderboard` then :func:`render_markdown`, wired to
``enterprise-sim bench report``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from enterprise_sim.benchmark.schema import Benchmark
from enterprise_sim.benchmark.score import Aggregate, Predictions, Report, score

# Name of the auto-derived floor runner added to every leaderboard.
BASELINE_NAME = "baseline"


# --------------------------------------------------------------------------- #
# The trivial baseline: always guess the most common gold answer.
# --------------------------------------------------------------------------- #


def most_frequent_baseline(benchmark: Benchmark) -> Predictions:
    """A naive floor runner: predict the single most frequent gold node id.

    Counts how often each id appears across every pair's ``expected_ids`` and
    answers *every* question with the single most common one (ties broken
    lexicographically, so the baseline is deterministic). It ignores the question
    entirely — that's the point: it's the score "guess the modal answer" earns, a
    floor a real runner must clear. An empty benchmark yields no predictions.
    """
    counts: Counter[str] = Counter()
    for pair in benchmark:
        counts.update(pair.expected_ids)
    if not counts:
        return Predictions()

    # max() over (count, id) keys, negating count so the highest count wins while
    # the lexicographically smallest id breaks ties — fully deterministic.
    best_id = min(counts, key=lambda node_id: (-counts[node_id], node_id))
    return Predictions.from_mapping({pair.id: (best_id,) for pair in benchmark})


# --------------------------------------------------------------------------- #
# Leaderboard: every runner scored against one benchmark.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunnerResult:
    """One runner's graded :class:`Report` plus the name it appears under."""

    name: str
    report: Report


@dataclass(frozen=True)
class Leaderboard:
    """Several runners scored against a shared benchmark.

    Attributes:
        benchmark_size: The number of questions every runner was graded on.
        reasoning_types: The reasoning types present in the benchmark, sorted —
            the rows of the per-type breakdown.
        runners: The graded runners, ordered best overall macro-F1 first (ties
            broken by name) so the report reads top-down like a leaderboard.
    """

    benchmark_size: int
    reasoning_types: tuple[str, ...]
    runners: tuple[RunnerResult, ...]


def build_leaderboard(
    benchmark: Benchmark,
    predictions: Mapping[str, Predictions],
    *,
    include_baseline: bool = True,
    baseline_name: str = BASELINE_NAME,
) -> Leaderboard:
    """Score each named runner against ``benchmark`` and rank them.

    ``predictions`` maps a runner name (e.g. ``"graph"``, ``"rag"``) to its
    :class:`Predictions`. When ``include_baseline`` is set, the trivial
    :func:`most_frequent_baseline` runner is added under ``baseline_name`` (unless
    a runner already claims that name). Runners are ordered by overall macro-F1
    descending — ties broken by name — so the leaderboard reads best-first.
    """
    named = dict(predictions)
    if include_baseline and baseline_name not in named:
        named[baseline_name] = most_frequent_baseline(benchmark)

    results = [
        RunnerResult(name=name, report=score(benchmark, preds)) for name, preds in named.items()
    ]
    results.sort(key=lambda r: (-r.report.overall.macro_f1, r.name))

    reasoning_types = tuple(sorted({pair.reasoning_type for pair in benchmark}))
    return Leaderboard(
        benchmark_size=len(benchmark),
        reasoning_types=reasoning_types,
        runners=tuple(results),
    )


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    """Render a metric to three decimal places (matching the score CLI)."""
    return f"{value:.3f}"


def _markdown_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A GitHub-flavored markdown table (header + separator + body rows)."""
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _overall_rows(leaderboard: Leaderboard) -> list[list[str]]:
    """Rank rows for the overall leaderboard table."""
    rows = []
    for rank, runner in enumerate(leaderboard.runners, start=1):
        agg = runner.report.overall
        rows.append(
            [
                str(rank),
                runner.name,
                _fmt(agg.macro_f1),
                _fmt(agg.macro_precision),
                _fmt(agg.macro_recall),
                _fmt(agg.exact_match_rate),
                str(agg.count),
            ]
        )
    return rows


def _by_type_rows(leaderboard: Leaderboard) -> list[list[str]]:
    """Per-reasoning-type macro-F1 rows, one column per runner.

    The best runner's F1 in each row is **bolded** so wins per reasoning type pop
    out (graph on ``transitive``, RAG on ``provenance``); ties bold every winner.
    """
    rows = []
    empty = Aggregate(0, 0.0, 0.0, 0.0, 0.0)
    for reasoning in leaderboard.reasoning_types:
        per_runner = [
            runner.report.by_reasoning_type.get(reasoning, empty) for runner in leaderboard.runners
        ]
        best_f1 = max((agg.macro_f1 for agg in per_runner), default=0.0)
        count = next((agg.count for agg in per_runner if agg.count), 0)
        cells = [reasoning, str(count)]
        for agg in per_runner:
            text = _fmt(agg.macro_f1)
            # Bold the winner(s); only when some runner actually scored above zero.
            if agg.macro_f1 == best_f1 and best_f1 > 0.0:
                text = f"**{text}**"
            cells.append(text)
        rows.append(cells)
    return rows


def render_markdown(leaderboard: Leaderboard, *, title: str = "KG-QA benchmark report") -> str:
    """Render a :class:`Leaderboard` as a markdown comparison report.

    Two tables: an overall leaderboard (runners ranked by macro-F1, with P/R/EM
    and question count) and a per-``reasoning_type`` macro-F1 breakdown with one
    column per runner. Pure: the same leaderboard always renders the same text.
    """
    runner_names = [runner.name for runner in leaderboard.runners]
    lines = [
        f"# {title}",
        "",
        f"Benchmark: {leaderboard.benchmark_size} questions · {len(leaderboard.runners)} runners.",
        "",
        "## Overall",
        "",
    ]
    overall_header = ["Rank", "Runner", "F1", "P", "R", "EM", "n"]
    lines.extend(_markdown_table(overall_header, _overall_rows(leaderboard)))

    lines.extend(["", "## By reasoning type", ""])
    if leaderboard.reasoning_types:
        lines.append("Macro-F1 per runner; **bold** marks the leader in each row.")
        lines.append("")
        by_type_header = ["reasoning_type", "n", *runner_names]
        lines.extend(_markdown_table(by_type_header, _by_type_rows(leaderboard)))
    else:
        lines.append("_No questions in the benchmark._")

    return "\n".join(lines) + "\n"


def build_report(
    benchmark: Benchmark,
    predictions: Mapping[str, Predictions],
    *,
    include_baseline: bool = True,
    title: str = "KG-QA benchmark report",
) -> str:
    """Build the leaderboard and render it to markdown in one call."""
    leaderboard = build_leaderboard(benchmark, predictions, include_baseline=include_baseline)
    return render_markdown(leaderboard, title=title)
