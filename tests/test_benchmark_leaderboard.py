"""Comparison-report tests (esim-uzc.6): the multi-runner markdown leaderboard.

The report stacks several runners against one benchmark and renders a markdown
leaderboard — overall macro-F1 per runner plus a per-``reasoning_type``
breakdown — always including a trivial most-frequent baseline. These tests pin
the baseline's behavior, the ranking, and the rendered markdown structure on
synthetic predictions. Like the scorer it is pure and keyless: the same inputs
always render the same text.
"""

from __future__ import annotations

from enterprise_sim.benchmark import (
    BASELINE_NAME,
    Benchmark,
    Predictions,
    QAPair,
    build_leaderboard,
    build_report,
    most_frequent_baseline,
    render_markdown,
)


def _pair(qid: str, expected: tuple[str, ...], reasoning: str = "direct_relation") -> QAPair:
    return QAPair(
        id=qid,
        question=f"q-{qid}?",
        qtype="who",
        reasoning_type=reasoning,
        expected_ids=expected,
    )


def _bench() -> Benchmark:
    return Benchmark.of(
        [
            _pair("q1", ("a",), reasoning="direct_relation"),
            _pair("q2", ("b",), reasoning="direct_relation"),
            _pair("q3", ("a",), reasoning="aggregation"),
        ]
    )


# -- baseline ----------------------------------------------------------------


def test_baseline_predicts_the_single_most_frequent_id() -> None:
    # "a" appears twice (q1, q3), "b" once -> baseline always guesses "a".
    baseline = most_frequent_baseline(_bench())
    assert baseline.ids_for("q1") == frozenset({"a"})
    assert baseline.ids_for("q2") == frozenset({"a"})
    assert baseline.ids_for("q3") == frozenset({"a"})


def test_baseline_breaks_ties_lexicographically() -> None:
    # "a" and "b" tie at one each -> deterministic lexicographic winner "a".
    bench = Benchmark.of([_pair("q1", ("b",)), _pair("q2", ("a",))])
    baseline = most_frequent_baseline(bench)
    assert baseline.ids_for("q1") == frozenset({"a"})


def test_baseline_of_empty_benchmark_is_empty() -> None:
    assert len(most_frequent_baseline(Benchmark())) == 0


# -- leaderboard construction ------------------------------------------------


def test_leaderboard_auto_adds_a_baseline_runner() -> None:
    board = build_leaderboard(_bench(), {"graph": Predictions()})
    names = {runner.name for runner in board.runners}
    assert names == {"graph", BASELINE_NAME}


def test_no_baseline_flag_omits_the_baseline() -> None:
    board = build_leaderboard(_bench(), {"graph": Predictions()}, include_baseline=False)
    assert [runner.name for runner in board.runners] == ["graph"]


def test_runners_ranked_by_overall_f1_descending() -> None:
    bench = _bench()
    perfect = Predictions.from_mapping({"q1": ["a"], "q2": ["b"], "q3": ["a"]})
    wrong = Predictions.from_mapping({"q1": ["z"], "q2": ["z"], "q3": ["z"]})
    board = build_leaderboard(bench, {"good": perfect, "bad": wrong}, include_baseline=False)
    assert [runner.name for runner in board.runners] == ["good", "bad"]
    assert board.runners[0].report.overall.macro_f1 == 1.0


def test_leaderboard_records_benchmark_size_and_reasoning_types() -> None:
    board = build_leaderboard(_bench(), {"graph": Predictions()})
    assert board.benchmark_size == 3
    assert board.reasoning_types == ("aggregation", "direct_relation")


# -- markdown rendering ------------------------------------------------------


def test_report_has_two_tables_and_a_title() -> None:
    bench = _bench()
    preds = Predictions.from_mapping({"q1": ["a"], "q2": ["b"], "q3": ["a"]})
    md = build_report(bench, {"graph": preds})
    assert md.startswith("# KG-QA benchmark report")
    assert "## Overall" in md
    assert "## By reasoning type" in md
    # Markdown table separator rows are present.
    assert "| --- |" in md
    # The baseline column header shows up in the by-type table.
    assert BASELINE_NAME in md


def test_overall_table_lists_each_runner_with_metrics() -> None:
    bench = _bench()
    preds = Predictions.from_mapping({"q1": ["a"], "q2": ["b"], "q3": ["a"]})
    md = build_report(bench, {"graph": preds}, include_baseline=False)
    overall = next(line for line in md.splitlines() if line.startswith("| 1 |"))
    assert "graph" in overall
    assert "1.000" in overall  # perfect runner


def test_by_type_table_bolds_the_row_leader() -> None:
    bench = _bench()
    # graph nails the direct_relation pairs; baseline ("a") only gets q1+q3.
    graph = Predictions.from_mapping({"q1": ["a"], "q2": ["b"], "q3": ["a"]})
    md = build_report(bench, {"graph": graph})
    direct_row = next(line for line in md.splitlines() if line.startswith("| direct_relation |"))
    # graph perfect on direct_relation, baseline imperfect -> graph bolded.
    assert "**1.000**" in direct_row


def test_render_is_deterministic() -> None:
    bench = _bench()
    preds = {"graph": Predictions.from_mapping({"q1": ["a"], "q2": ["b"], "q3": ["a"]})}
    board = build_leaderboard(bench, preds)
    assert render_markdown(board) == render_markdown(board)


def test_empty_benchmark_renders_without_breakdown() -> None:
    board = build_leaderboard(Benchmark(), {"graph": Predictions()})
    md = render_markdown(board)
    assert "# KG-QA benchmark report" in md
    assert "_No questions in the benchmark._" in md


def test_report_trailing_newline() -> None:
    md = build_report(_bench(), {"graph": Predictions()})
    assert md.endswith("\n")
