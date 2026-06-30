"""Report-rendering tests (esim-uzc.7): :func:`format_report` human output.

The grader produces a :class:`~enterprise_sim.benchmark.score.Report`; the CLI
renders it for humans through :func:`~enterprise_sim.benchmark.score.format_report`.
These tests pin that rendering — the headline overall line, the per-
``reasoning_type`` breakdown, numeric formatting, and the degenerate empty
report — so the score CLI's output stays stable. Rendering is pure and keyless:
the same :class:`Report` always renders to the same text.
"""

from __future__ import annotations

from enterprise_sim.benchmark import (
    Benchmark,
    Predictions,
    QAPair,
    format_report,
    score,
)


def _pair(qid: str, expected: tuple[str, ...], reasoning: str = "direct_relation") -> QAPair:
    return QAPair(
        id=qid,
        question=f"q-{qid}?",
        qtype="who",
        reasoning_type=reasoning,
        expected_ids=expected,
    )


def _render(bench: Benchmark, mapping: dict[str, list[str]]) -> str:
    return format_report(score(bench, Predictions.from_mapping(mapping)))


# -- structure ---------------------------------------------------------------


def test_render_has_title_and_overall_line() -> None:
    text = _render(Benchmark.of([_pair("q1", ("a",))]), {"q1": ["a"]})
    lines = text.splitlines()
    assert lines[0] == "KG-QA benchmark score"
    assert lines[1].strip().startswith("overall")


def test_render_lists_each_reasoning_type() -> None:
    bench = Benchmark.of(
        [
            _pair("q1", ("a",), reasoning="direct_relation"),
            _pair("q2", ("b",), reasoning="aggregation"),
        ]
    )
    text = _render(bench, {"q1": ["a"], "q2": ["b"]})
    assert "by reasoning_type:" in text
    assert "direct_relation" in text
    assert "aggregation" in text


def test_reasoning_type_lines_are_sorted() -> None:
    bench = Benchmark.of(
        [
            _pair("q1", ("a",), reasoning="provenance"),
            _pair("q2", ("b",), reasoning="aggregation"),
            _pair("q3", ("c",), reasoning="direct_relation"),
        ]
    )
    text = _render(bench, {"q1": ["a"], "q2": ["b"], "q3": ["c"]})
    order = [line.strip().split()[0] for line in text.splitlines() if line.startswith("  ")]
    # "overall" first, then the reasoning types in sorted order.
    assert order == ["overall", "aggregation", "direct_relation", "provenance"]


# -- formatting --------------------------------------------------------------


def test_overall_line_reports_all_metrics() -> None:
    # one perfect, one wrong -> F1 0.5, EM 0.5, n=2.
    bench = Benchmark.of([_pair("q1", ("a",)), _pair("q2", ("b",))])
    text = _render(bench, {"q1": ["a"], "q2": ["z"]})
    overall = next(line for line in text.splitlines() if line.strip().startswith("overall"))
    assert "n=2" in overall
    assert "F1=0.500" in overall
    assert "P=0.500" in overall
    assert "R=0.500" in overall
    assert "EM=0.500" in overall


def test_metrics_render_with_three_decimals() -> None:
    # macro F1 of 2/3 must render rounded to three places.
    bench = Benchmark.of([_pair("q1", ("a", "b", "c"))])
    text = _render(bench, {"q1": ["a", "b", "x"]})
    assert "F1=0.667" in text


def test_perfect_score_renders_ones() -> None:
    text = _render(Benchmark.of([_pair("q1", ("a",))]), {"q1": ["a"]})
    assert "F1=1.000" in text
    assert "EM=1.000" in text


# -- empty report ------------------------------------------------------------


def test_empty_report_renders_zeros_and_omits_breakdown() -> None:
    text = format_report(score(Benchmark(), Predictions()))
    lines = text.splitlines()
    assert lines[0] == "KG-QA benchmark score"
    assert "n=0" in lines[1]
    assert "F1=0.000" in lines[1]
    # No questions -> no per-reasoning-type section.
    assert "by reasoning_type:" not in text


# -- purity ------------------------------------------------------------------


def test_rendering_is_deterministic() -> None:
    bench = Benchmark.of([_pair("q1", ("a",)), _pair("q2", ("b",), reasoning="aggregation")])
    report = score(bench, Predictions.from_mapping({"q1": ["a"], "q2": ["b"]}))
    assert format_report(report) == format_report(report)
