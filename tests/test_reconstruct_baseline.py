"""Keyless coverage for score baselines + ``reconstruct baseline`` (spec 0003 slice 2).

Two layers: the compare semantics (``exact`` equality at 6 dp, ``warn`` tolerance
band, ``--strict``, the unseeded-keyed skip) exercised on in-memory cells so they run
in microseconds, plus one end-to-end test that regenerates the committed
``golden-fake`` cell keylessly and asserts it matches the file byte-for-byte — the
gate's guard that a silent fidelity move fails on the PR that causes it. The
regeneration reuses the ~0.8s e2e smoke path, so the gate budget holds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import build_parser, main
from enterprise_sim.reconstruct.baseline import (
    FAKE_CELLS,
    FIDELITY_METRIC_KEYS,
    BaselineCell,
    CellSpec,
    build_cell,
    cell_path,
    compare,
    regenerate_fake_metrics,
)


def _cell(mode: str, tolerance: float, metrics: dict[str, float | int]) -> BaselineCell:
    """A minimal in-memory cell for compare-semantics tests."""
    return BaselineCell(
        cell="unit",
        backend="fake",
        source="unit",
        config="examples/golden.toml",
        seed=7,
        mode=mode,
        tolerance=tolerance,
        metrics=metrics,
        reason="unit",
    )


def test_exact_compare_matches_and_rounds() -> None:
    cell = _cell("exact", 0.0, {"node_f1": 0.311111, "over_merges": 0})
    # Identical values compare clean.
    assert compare(cell, {"node_f1": 0.311111, "over_merges": 0}).ok
    # Sub-6dp jitter (summation reorder) is rounded away, not flagged.
    assert compare(cell, {"node_f1": 0.311111_4, "over_merges": 0}).ok


def test_exact_compare_fails_at_6dp() -> None:
    cell = _cell("exact", 0.0, {"node_f1": 0.311111, "edge_f1": 0.345679})
    result = compare(cell, {"node_f1": 0.312000, "edge_f1": 0.345679})
    assert not result.ok
    exceeded = result.exceedances
    assert [d.metric for d in exceeded] == ["node_f1"]
    # The drift carries the numbers the CLI message names.
    drift = exceeded[0]
    assert drift.expected == pytest.approx(0.311111)
    assert drift.actual == pytest.approx(0.312000)
    assert drift.delta == pytest.approx(0.000889)


def test_exact_compare_flags_missing_metric() -> None:
    cell = _cell("exact", 0.0, {"node_f1": 0.311111})
    result = compare(cell, {})
    assert not result.ok
    assert result.exceedances[0].metric == "node_f1"


def test_warn_compare_tolerance_band() -> None:
    cell = _cell("warn", 0.05, {"oracle_f1": 0.50})
    # Within the band: clean.
    assert compare(cell, {"oracle_f1": 0.54}).ok
    # Beyond the band: an exceedance (warn mode; the CLI downgrades it without --strict).
    result = compare(cell, {"oracle_f1": 0.60})
    assert not result.ok
    assert result.exceedances[0].metric == "oracle_f1"


def test_baseline_cell_roundtrips_with_schema_alias(tmp_path: Path) -> None:
    """The on-disk key is ``schema`` (aliased), and to_json is sorted + newline-terminated."""
    cell = _cell("exact", 0.0, {"node_f1": 0.5, "over_merges": 0})
    text = cell.to_json()
    parsed = json.loads(text)
    assert parsed["schema"] == 1
    assert text.endswith("}\n")
    # ints stay ints, floats stay floats through the round-trip.
    assert parsed["metrics"]["over_merges"] == 0
    path = tmp_path / "unit.json"
    path.write_text(text, encoding="utf-8")
    assert BaselineCell.read(path).metrics == {"node_f1": 0.5, "over_merges": 0}


def test_regenerate_fake_metrics_matches_committed_golden_cell() -> None:
    """The committed golden-fake cell equals a fresh keyless regeneration (D10/D31)."""
    spec = FAKE_CELLS["golden-fake"]
    committed = BaselineCell.read(cell_path("golden-fake"))
    assert set(committed.metrics) == set(FIDELITY_METRIC_KEYS)

    current = regenerate_fake_metrics(spec)
    assert compare(committed, current).ok
    # A rebuilt cell (identity from code, metrics from regen) reproduces the file byte
    # for byte except reason/source, which are stamped identically — so `update` on a
    # clean checkout diffs only the reason.
    rebuilt = build_cell(spec, current, committed.reason)
    assert rebuilt.to_json() == cell_path("golden-fake").read_text(encoding="utf-8")


def test_regenerate_matrix_cell_matches_committed_file() -> None:
    """The committed matrix-fake cell equals a fresh keyless 6-cell regeneration (D10/D31).

    The full seeds matrix (~0.4s in-process, well inside the ~5s gate budget measured
    in slice 3) is regenerated and byte-compared, so a silent per-cell fidelity move
    fails on the PR that causes it — the same guard the golden cell carries.
    """
    spec = FAKE_CELLS["matrix-fake"]
    committed = BaselineCell.read(cell_path("matrix-fake"))
    # 6 cells × (6 rate + 4 count/merge) + 6 aggregate means = 66 pinned metrics.
    assert len(committed.metrics) == 66

    current = regenerate_fake_metrics(spec)
    assert compare(committed, current).ok
    rebuilt = build_cell(spec, current, committed.reason)
    assert rebuilt.to_json() == cell_path("matrix-fake").read_text(encoding="utf-8")


def test_regenerate_refuses_keyed_cell() -> None:
    keyed = CellSpec(
        cell="golden-keyed",
        backend="anthropic_api",
        config="examples/golden.toml",
        seed=7,
        mode="warn",
        tolerance=0.05,
        keyed=True,
    )
    with pytest.raises(ValueError, match="keyed"):
        regenerate_fake_metrics(keyed)


def test_baseline_check_all_passes_on_clean_checkout(capsys: Any) -> None:
    rc = main(["reconstruct", "baseline", "check", "--cell", "all"])
    assert rc == 0
    assert "golden-fake" in capsys.readouterr().err


def test_baseline_check_fails_and_names_metric_on_perturbation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A perturbed exact cell exits non-zero, naming metric/expected/actual/tolerance."""
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)

    good = build_cell(
        FAKE_CELLS["golden-fake"], regenerate_fake_metrics(FAKE_CELLS["golden-fake"]), "unit"
    )
    perturbed = good.model_copy(update={"metrics": {**good.metrics, "node_f1": 0.999999}})
    (baselines / "golden-fake.json").write_text(perturbed.to_json(), encoding="utf-8")

    rc = main(["reconstruct", "baseline", "check", "--cell", "golden-fake"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err
    assert "node_f1" in err
    assert "expected 0.999999" in err
    assert "tolerance 0.000000" in err


def test_baseline_check_skips_unseeded_keyed_cell(capsys: Any) -> None:
    rc = main(["reconstruct", "baseline", "check", "--cell", "golden-keyed"])
    assert rc == 0
    assert "unseeded" in capsys.readouterr().err


def test_baseline_update_refuses_without_reason(capsys: Any) -> None:
    rc = main(["reconstruct", "baseline", "update", "--cell", "golden-fake"])
    assert rc == 2
    assert "--reason is required" in capsys.readouterr().err


def test_baseline_update_rewrites_only_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update on a clean cell changes only the reason (metrics regenerate identical)."""
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)

    assert (
        main(["reconstruct", "baseline", "update", "--cell", "golden-fake", "--reason", "a"]) == 0
    )
    first = (baselines / "golden-fake.json").read_text(encoding="utf-8")
    assert (
        main(["reconstruct", "baseline", "update", "--cell", "golden-fake", "--reason", "b"]) == 0
    )
    second = (baselines / "golden-fake.json").read_text(encoding="utf-8")
    assert first.replace('"a"', '"b"') == second


def test_baseline_subcommand_arg_surface() -> None:
    parser = build_parser()
    check = parser.parse_args(["reconstruct", "baseline", "check"])
    assert check.cell == "all"
    assert check.against is None
    assert check.strict is False
    update = parser.parse_args(
        ["reconstruct", "baseline", "update", "--cell", "golden-fake", "--reason", "x"]
    )
    assert update.cell == "golden-fake"
    assert update.reason == "x"
