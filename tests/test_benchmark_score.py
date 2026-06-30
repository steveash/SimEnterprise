"""Grader/scorer tests (esim-uzc.3): set-based P/R/F1 and macro aggregation.

Covers :mod:`enterprise_sim.benchmark.score`:

* :class:`Prediction` / :class:`Predictions` JSONL round-trip and lookups;
* per-item scoring — perfect predictions score F1 1.0, partial overlap yields
  the correct precision/recall/F1, an empty/missing prediction scores 0;
* macro aggregation overall and per ``reasoning_type``; and
* the ``enterprise-sim bench score --bench … --pred …`` CLI.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark import Benchmark, QAPair
from enterprise_sim.benchmark.score import (
    Aggregate,
    Prediction,
    Predictions,
    score,
    score_item,
)
from enterprise_sim.cli import main


def _pair(qid: str, expected: tuple[str, ...], reasoning: str = "direct_relation") -> QAPair:
    return QAPair(
        id=qid,
        question=f"q-{qid}?",
        qtype="who",
        reasoning_type=reasoning,
        expected_ids=expected,
    )


# -- Prediction / Predictions ------------------------------------------------


def test_prediction_jsonl_round_trip() -> None:
    pred = Prediction(qa_id="qa-1", predicted_ids=("a", "b"))
    line = pred.to_jsonl()
    assert "\n" not in line
    assert Prediction.from_jsonl(line) == pred


def test_prediction_normalizes_list_to_tuple() -> None:
    pred = Prediction(qa_id="qa-1", predicted_ids=["a", "b"])  # type: ignore[arg-type]
    assert pred.predicted_ids == ("a", "b")
    assert pred in {pred}  # hashable


def test_predictions_jsonl_file_round_trip(tmp_path: Path) -> None:
    preds = Predictions.of([Prediction("qa-1", ("a", "b")), Prediction("qa-2", ("c",))])
    path = tmp_path / "pred.jsonl"
    preds.write_jsonl(path)

    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == 2

    loaded = Predictions.read_jsonl(path)
    assert loaded.ids_for("qa-1") == {"a", "b"}
    assert loaded.ids_for("qa-2") == {"c"}


def test_predictions_missing_id_is_empty_set() -> None:
    preds = Predictions.of([Prediction("qa-1", ("a",))])
    assert preds.ids_for("absent") == frozenset()


def test_predictions_from_mapping_and_last_row_wins() -> None:
    assert Predictions.from_mapping({"qa-1": ["a", "b"]}).ids_for("qa-1") == {"a", "b"}
    dupes = Predictions.of([Prediction("qa-1", ("a",)), Prediction("qa-1", ("b",))])
    assert dupes.ids_for("qa-1") == {"b"}


def test_predictions_from_jsonl_skips_blank_lines() -> None:
    text = f"{Prediction('a', ('x',)).to_jsonl()}\n\n{Prediction('b', ('y',)).to_jsonl()}\n"
    preds = Predictions.from_jsonl(text)
    assert preds.ids_for("a") == {"x"}
    assert preds.ids_for("b") == {"y"}


def test_empty_predictions_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    Predictions().write_jsonl(path)
    assert path.read_text(encoding="utf-8") == ""
    assert len(Predictions.read_jsonl(path)) == 0


# -- per-item scoring --------------------------------------------------------


def test_perfect_prediction_scores_f1_one() -> None:
    item = score_item(_pair("q", ("a", "b")), frozenset({"a", "b"}))
    assert item.exact_match is True
    assert item.precision == 1.0
    assert item.recall == 1.0
    assert item.f1 == 1.0


def test_perfect_prediction_ignores_order_and_duplicates() -> None:
    # predicted as a set; the gold is multi-id — order is irrelevant.
    item = score_item(_pair("q", ("b", "a")), frozenset({"a", "b"}))
    assert item.exact_match is True
    assert item.f1 == 1.0


def test_partial_overlap_precision_recall_f1() -> None:
    # expected {a,b,c}; predicted {a,b,x}: tp=2, p=2/3, r=2/3, f1=2/3.
    item = score_item(_pair("q", ("a", "b", "c")), frozenset({"a", "b", "x"}))
    assert item.exact_match is False
    assert math.isclose(item.precision, 2 / 3)
    assert math.isclose(item.recall, 2 / 3)
    assert math.isclose(item.f1, 2 / 3)


def test_asymmetric_partial_overlap() -> None:
    # expected {a,b}; predicted {a,x,y,z}: tp=1, p=1/4, r=1/2, f1=2pr/(p+r)=1/3.
    item = score_item(_pair("q", ("a", "b")), frozenset({"a", "x", "y", "z"}))
    assert math.isclose(item.precision, 1 / 4)
    assert math.isclose(item.recall, 1 / 2)
    assert math.isclose(item.f1, 1 / 3)


def test_empty_prediction_scores_zero() -> None:
    item = score_item(_pair("q", ("a", "b")), frozenset())
    assert item.exact_match is False
    assert item.precision == 0.0
    assert item.recall == 0.0
    assert item.f1 == 0.0


def test_disjoint_prediction_scores_zero() -> None:
    item = score_item(_pair("q", ("a",)), frozenset({"z"}))
    assert item.exact_match is False
    assert item.f1 == 0.0


def test_item_records_sorted_expected_and_predicted() -> None:
    item = score_item(_pair("q", ("b", "a")), frozenset({"y", "x"}))
    assert item.expected == ("a", "b")
    assert item.predicted == ("x", "y")


# -- aggregation -------------------------------------------------------------


def test_aggregate_over_empty_is_zero() -> None:
    agg = Aggregate.over([])
    assert agg == Aggregate(0, 0.0, 0.0, 0.0, 0.0)


def test_score_perfect_benchmark_macro_f1_one() -> None:
    bench = Benchmark.of([_pair("q1", ("a",)), _pair("q2", ("b", "c"))])
    preds = Predictions.from_mapping({"q1": ["a"], "q2": ["b", "c"]})
    report = score(bench, preds)
    assert report.overall.count == 2
    assert report.overall.macro_f1 == 1.0
    assert report.overall.exact_match_rate == 1.0


def test_score_missing_prediction_counts_as_zero() -> None:
    bench = Benchmark.of([_pair("q1", ("a",)), _pair("q2", ("b",))])
    preds = Predictions.from_mapping({"q1": ["a"]})  # q2 unanswered
    report = score(bench, preds)
    # one perfect, one zero -> macro F1 0.5, EM 0.5.
    assert math.isclose(report.overall.macro_f1, 0.5)
    assert math.isclose(report.overall.exact_match_rate, 0.5)


def test_score_macro_f1_averages_per_item() -> None:
    # item1 perfect (f1=1), item2 partial (f1=2/3) -> macro f1 = (1 + 2/3)/2.
    bench = Benchmark.of([_pair("q1", ("a",)), _pair("q2", ("a", "b", "c"))])
    preds = Predictions.from_mapping({"q1": ["a"], "q2": ["a", "b", "x"]})
    report = score(bench, preds)
    assert math.isclose(report.overall.macro_f1, (1.0 + 2 / 3) / 2)


def test_score_per_reasoning_type_aggregation() -> None:
    bench = Benchmark.of(
        [
            _pair("q1", ("a",), reasoning="direct_relation"),
            _pair("q2", ("b",), reasoning="direct_relation"),
            _pair("q3", ("c", "d"), reasoning="aggregation"),
        ]
    )
    # direct_relation: q1 perfect, q2 wrong -> macro f1 0.5; aggregation: perfect.
    preds = Predictions.from_mapping({"q1": ["a"], "q2": ["z"], "q3": ["c", "d"]})
    report = score(bench, preds)

    assert set(report.by_reasoning_type) == {"direct_relation", "aggregation"}
    direct = report.by_reasoning_type["direct_relation"]
    aggregation = report.by_reasoning_type["aggregation"]
    assert direct.count == 2
    assert math.isclose(direct.macro_f1, 0.5)
    assert math.isclose(direct.exact_match_rate, 0.5)
    assert aggregation.count == 1
    assert aggregation.macro_f1 == 1.0


def test_score_iterates_benchmark_order_and_ignores_extra_predictions() -> None:
    bench = Benchmark.of([_pair("q1", ("a",))])
    preds = Predictions.from_mapping({"q1": ["a"], "q-extra": ["junk"]})
    report = score(bench, preds)
    assert [it.qa_id for it in report.items] == ["q1"]
    assert report.overall.count == 1


def test_empty_benchmark_scores_empty_report() -> None:
    report = score(Benchmark(), Predictions())
    assert report.items == ()
    assert report.overall.count == 0
    assert report.by_reasoning_type == {}


# -- CLI ---------------------------------------------------------------------


def test_bench_score_subcommand_registered() -> None:
    from enterprise_sim.cli import build_parser

    args = build_parser().parse_args(["bench", "score", "--bench", "b.jsonl", "--pred", "p.jsonl"])
    assert args.bench == Path("b.jsonl")
    assert args.pred == Path("p.jsonl")
    assert args.func is not None


def test_bench_score_cli_prints_report(tmp_path: Path, capsys: Any) -> None:
    bench = Benchmark.of([_pair("q1", ("a",)), _pair("q2", ("b", "c"))])
    bench_path = tmp_path / "bench.jsonl"
    bench.write_jsonl(bench_path)

    preds = Predictions.from_mapping({"q1": ["a"], "q2": ["b"]})
    pred_path = tmp_path / "pred.jsonl"
    preds.write_jsonl(pred_path)

    code = main(["bench", "score", "--bench", str(bench_path), "--pred", str(pred_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "overall" in out
    assert "direct_relation" in out
    assert "F1=" in out


def test_bench_score_cli_requires_both_paths(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bench", "score", "--bench", "only.jsonl"])
    assert exc.value.code == 2
