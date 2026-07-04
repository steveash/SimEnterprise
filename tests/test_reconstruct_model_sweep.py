"""Model-axis sweep tests (esim-ecr.4).

Covers the model-sweep harness the acceptance criteria name:

* **Iteration + ordering** — :func:`sweep_models` reconstructs the corpus once per
  model, in input order, de-duplicating repeated models.
* **Fidelity table** — every model row carries node/edge P/R/F1 and recon/gold
  sizes; the edge-F1 leader is the fidelity headline (ties break to the earlier
  input model).
* **Optional answer-F1** — with an injected ``answer_scorer`` (the CLI wires the
  keyed graph agent) each row also carries answer-F1, and the answer-F1 leader is
  reported; without one the sweep scores fidelity only.
* **Keyless end to end** — the sweep runs over a fresh golden-run reconstruction via
  the fake backend with no key; the fake backend invents meaningless entities that
  never match the gold graph, so every model scores the same degenerate fidelity —
  the model is a recorded label in keyless CI, real numbers are a keyed run.
* **CLI** — ``reconstruct sweep --models`` is registered, parses the model list, and
  writes a loadable per-model table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.score import Aggregate, Report
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.llm import LLMConfig, build_client
from enterprise_sim.reconstruct import (
    EdgeFidelity,
    EntityResolution,
    FidelityReport,
    ModelPoint,
    ModelSweepReport,
    NodeFidelity,
)
from enterprise_sim.reconstruct.fidelity import PRF
from enterprise_sim.reconstruct.model_sweep import sweep_models

# --------------------------------------------------------------------------- #
# Fabricated fidelity / answer reports (no run needed for the report-shape tests).
# --------------------------------------------------------------------------- #


def _prf(tp: int, predicted: int, gold: int) -> PRF:
    return PRF(true_positives=tp, predicted=predicted, gold=gold)


def _fidelity(*, edge_tp: int, edge_pred: int, edge_gold: int = 4) -> FidelityReport:
    """A fidelity report with perfect nodes and a controllable edge PRF."""
    return FidelityReport(
        nodes=NodeFidelity(
            overall=_prf(4, 4, 4),
            by_type={},
            alignment={},
            unmatched_reconstructed=(),
            unmatched_gold=(),
        ),
        edges=EdgeFidelity(overall=_prf(edge_tp, edge_pred, edge_gold), by_type={}),
        entity_resolution=EntityResolution(over_merges=0, under_merges=0),
        reconstructed_node_count=4,
        gold_node_count=4,
        reconstructed_edge_count=edge_pred,
        gold_edge_count=edge_gold,
    )


def _answer(macro_f1: float, *, count: int = 3) -> Report:
    """An answer report whose only load-bearing field is the overall macro-F1."""
    overall = Aggregate(
        count=count,
        exact_match_rate=macro_f1,
        macro_precision=macro_f1,
        macro_recall=macro_f1,
        macro_f1=macro_f1,
    )
    return Report(items=(), overall=overall, by_reasoning_type={})


# --------------------------------------------------------------------------- #
# Report shape: best_* selection + tie-breaking.
# --------------------------------------------------------------------------- #


def test_best_edge_f1_picks_the_leader() -> None:
    report = ModelSweepReport(
        points=[
            ModelPoint("haiku", _fidelity(edge_tp=2, edge_pred=4)),  # edge F1 = 0.5
            ModelPoint("sonnet", _fidelity(edge_tp=4, edge_pred=4)),  # edge F1 = 1.0
        ],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
    )
    best = report.best_edge_f1()
    assert best is not None
    assert best.model == "sonnet"


def test_best_edge_f1_breaks_ties_toward_the_earlier_model() -> None:
    # Both models tie on edge F1; the earlier input model wins the tie.
    report = ModelSweepReport(
        points=[
            ModelPoint("haiku", _fidelity(edge_tp=4, edge_pred=4)),
            ModelPoint("sonnet", _fidelity(edge_tp=4, edge_pred=4)),
        ],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
    )
    best = report.best_edge_f1()
    assert best is not None
    assert best.model == "haiku"


def test_best_answer_f1_is_none_without_scoring() -> None:
    report = ModelSweepReport(
        points=[ModelPoint("haiku", _fidelity(edge_tp=4, edge_pred=4))],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
    )
    assert not report.scored_answers
    assert report.best_answer_f1() is None


def test_best_answer_f1_picks_the_answer_leader() -> None:
    report = ModelSweepReport(
        points=[
            ModelPoint("haiku", _fidelity(edge_tp=4, edge_pred=4), _answer(0.4)),
            ModelPoint("sonnet", _fidelity(edge_tp=2, edge_pred=4), _answer(0.7)),
        ],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
        benchmark_size=3,
    )
    assert report.scored_answers
    best = report.best_answer_f1()
    assert best is not None
    assert best.model == "sonnet"
    assert best.answer_f1 == pytest.approx(0.7)


# --------------------------------------------------------------------------- #
# Serialization.
# --------------------------------------------------------------------------- #


def test_report_json_carries_backend_and_best_models() -> None:
    report = ModelSweepReport(
        points=[
            ModelPoint("haiku", _fidelity(edge_tp=2, edge_pred=4), _answer(0.4)),
            ModelPoint("sonnet", _fidelity(edge_tp=4, edge_pred=4), _answer(0.7)),
        ],
        gold_node_count=4,
        gold_edge_count=4,
        backend="anthropic_api",
        benchmark_size=3,
    )
    data = json.loads(report.to_json())
    assert data["backend"] == "anthropic_api"
    assert data["benchmark_size"] == 3
    assert data["best_edge_f1_model"] == "sonnet"
    assert data["best_answer_f1_model"] == "sonnet"
    assert [p["model"] for p in data["points"]] == ["haiku", "sonnet"]
    # Answer numbers ride along when scored.
    assert data["points"][0]["answer"]["macro_f1"] == pytest.approx(0.4)


def test_report_json_omits_answer_when_unscored() -> None:
    report = ModelSweepReport(
        points=[ModelPoint("haiku", _fidelity(edge_tp=4, edge_pred=4))],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
    )
    data = json.loads(report.to_json())
    assert data["benchmark_size"] is None
    assert data["best_answer_f1_model"] is None
    assert "answer" not in data["points"][0]


def test_markdown_has_a_row_per_model_and_callouts() -> None:
    report = ModelSweepReport(
        points=[
            ModelPoint("haiku", _fidelity(edge_tp=2, edge_pred=4), _answer(0.4)),
            ModelPoint("sonnet", _fidelity(edge_tp=4, edge_pred=4), _answer(0.7)),
        ],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
        benchmark_size=3,
    )
    md = report.to_markdown()
    assert "# Reconstruct model sweep" in md
    assert "| model |" in md
    assert "answer F1" in md
    assert "haiku" in md and "sonnet" in md  # both models are rows
    assert "**Best edge F1:** 1.000 by `sonnet`" in md
    assert "**Best answer F1:** 0.700 by `sonnet`" in md
    # header + separator + one row per model.
    assert md.count("\n| ") >= 2 + 2


def test_markdown_drops_answer_columns_when_unscored() -> None:
    report = ModelSweepReport(
        points=[ModelPoint("haiku", _fidelity(edge_tp=4, edge_pred=4))],
        gold_node_count=4,
        gold_edge_count=4,
        backend="fake",
    )
    md = report.to_markdown()
    assert "answer F1" not in md
    assert "**Best answer F1:**" not in md
    assert "**Best edge F1:**" in md


# --------------------------------------------------------------------------- #
# Harness: iteration + ordering (keyless, fake backend).
# --------------------------------------------------------------------------- #


def _golden_run(root: Path) -> tuple[str, Any]:
    """A fresh golden run: its run dir (raw corpus + gold kg/) and gold world."""
    from enterprise_sim.benchmark.fixtures import golden_run

    run = golden_run(root)
    return str(run.run_dir), run.world


def test_sweep_models_iterates_in_order_and_dedupes(tmp_path: Path) -> None:
    run_dir, gold = _golden_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    report = sweep_models(
        run_dir,
        gold,
        ["haiku", "sonnet", "haiku"],  # duplicate is dropped, order preserved
        client,
    )
    assert [p.model for p in report.points] == ["haiku", "sonnet"]
    assert report.gold_node_count > 0
    assert not report.scored_answers  # no scorer → fidelity only
    assert all(p.answer is None for p in report.points)


def test_sweep_models_rejects_empty_model_list(tmp_path: Path) -> None:
    run_dir, gold = _golden_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    with pytest.raises(ValueError, match="at least one model"):
        sweep_models(run_dir, gold, [], client)


def test_sweep_models_fake_backend_yields_degenerate_fidelity_per_model(tmp_path: Path) -> None:
    # The fake backend invents meaningless entities that never match the gold graph,
    # so every model scores the same degenerate (~0) fidelity — the model axis is a
    # recorded label in keyless CI; real per-model differences are a keyed run.
    run_dir, gold = _golden_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    report = sweep_models(run_dir, gold, ["haiku", "sonnet"], client)
    haiku, sonnet = report.points
    assert haiku.node_f1 == sonnet.node_f1 == 0.0
    assert haiku.edge_f1 == sonnet.edge_f1 == 0.0
    assert haiku.fidelity.reconstructed_node_count == sonnet.fidelity.reconstructed_node_count


def test_sweep_models_runs_the_injected_answer_scorer(tmp_path: Path) -> None:
    run_dir, gold = _golden_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))

    seen: list[str] = []

    def fake_scorer(kg: Any, model: str) -> Report:
        seen.append(model)
        # A stronger label "scores" higher, so the leader is deterministic.
        return _answer(0.7 if model == "sonnet" else 0.4)

    report = sweep_models(
        run_dir,
        gold,
        ["haiku", "sonnet"],
        client,
        answer_scorer=fake_scorer,
    )
    assert seen == ["haiku", "sonnet"]  # scorer runs once per model, in order
    assert report.scored_answers
    assert report.benchmark_size == 3
    best = report.best_answer_f1()
    assert best is not None and best.model == "sonnet"


def test_sweep_models_is_deterministic(tmp_path: Path) -> None:
    run_dir, gold = _golden_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    a = sweep_models(run_dir, gold, ["haiku", "sonnet"], client)
    b = sweep_models(run_dir, gold, ["haiku", "sonnet"], client)
    assert a.to_json() == b.to_json()


def test_sweep_models_honors_the_edge_threshold(tmp_path: Path) -> None:
    from enterprise_sim.reconstruct import BuildConfig

    run_dir, gold = _golden_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    # A threshold above the max clamped confidence gates every edge away.
    report = sweep_models(
        run_dir,
        gold,
        ["haiku"],
        client,
        build_config=BuildConfig(edge_confidence_threshold=1.1),
    )
    assert report.points[0].fidelity.reconstructed_edge_count == 0


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def test_sweep_cli_parses_models() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["reconstruct", "sweep", "--models", "claude-haiku-4-5-20251001, claude-sonnet-4-6 ,"]
    )
    assert args.models == ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
    assert args.edge_threshold == 0.0


def test_sweep_cli_rejects_empty_models() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["reconstruct", "sweep", "--models", " , "])


def test_sweep_cli_model_axis_writes_markdown_table(tmp_path: Path, capsys: Any) -> None:
    run_dir, _gold = _golden_run(tmp_path / "run")
    out = tmp_path / "model-sweep.md"
    rc = main(
        [
            "reconstruct",
            "sweep",
            "--run",
            run_dir,
            "--models",
            "haiku,sonnet",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "Reconstruct model sweep" in text
    assert "| model |" in text
    assert "haiku" in text and "sonnet" in text
    err = capsys.readouterr().err
    assert "reconstruct sweep" in err
    assert "2 models" in err


def test_sweep_cli_model_axis_json_to_stdout(tmp_path: Path, capsys: Any) -> None:
    run_dir, _gold = _golden_run(tmp_path / "run")
    rc = main(["reconstruct", "sweep", "--run", run_dir, "--models", "haiku,sonnet", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["model"] for p in data["points"]] == ["haiku", "sonnet"]
    assert data["benchmark_size"] is None


def test_sweep_without_models_still_runs_the_threshold_axis(tmp_path: Path) -> None:
    # The model axis is additive: no --models keeps the ecr.3 threshold sweep.
    run_dir, _gold = _golden_run(tmp_path / "run")
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "sweep", "--run", run_dir])
    assert args.models is None
    rc = main(["reconstruct", "sweep", "--run", run_dir, "--thresholds", "0,1.1", "--json"])
    assert rc == 0
