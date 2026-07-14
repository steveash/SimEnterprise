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
    ANSWER_F1_METRIC_KEYS,
    FAKE_CELLS,
    FIDELITY_METRIC_KEYS,
    GAP_METRIC_KEYS,
    KEYED_CELLS,
    KEYED_METRIC_KEYS,
    BaselineCell,
    CellSpec,
    build_cell,
    cell_path,
    compare,
    expected_metric_keys,
    identity_mismatches,
    keyed_summary_problem,
    metrics_from_summary,
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


def _keyed_summary(*, backend: str = "anthropic_api", mode: str = "eval") -> dict[str, Any]:
    """A minimal valid keyed e2e summary payload for the keyed-cell tests."""
    return {
        "mode": mode,
        "backend": backend,
        "model": "claude-sonnet-4-6",
        "run_id": "golden-slice-co-6c66fbef69f8",
        "fidelity": {
            "node_f1": 0.5,
            "node_precision": 0.5,
            "node_recall": 0.5,
            "edge_f1": 0.5,
            "edge_precision": 0.5,
            "edge_recall": 0.5,
            "provenance_f1": 0.5,
            "over_merges": 0,
            "under_merges": 0,
            "reconstructed_nodes": 30,
            "reconstructed_edges": 30,
        },
        "answer_f1": {"oracle": 0.6, "reconstructed": 0.5, "rag": 0.4},
        "gaps": {"understanding": 0.1, "reasoning": 0.1, "total": 0.2},
    }


# --- F1: keyed cells pin fidelity + answer-F1 + gaps -------------------------------


def test_keyed_cell_metric_shape_and_keys() -> None:
    spec = KEYED_CELLS["golden-keyed"]
    assert spec.metrics_shape == "fidelity+answers"
    assert expected_metric_keys(spec) == KEYED_METRIC_KEYS
    # The keyed key set is fidelity + the three answer-F1 slots + the three gaps.
    assert set(KEYED_METRIC_KEYS) == set(FIDELITY_METRIC_KEYS) | set(ANSWER_F1_METRIC_KEYS) | set(
        GAP_METRIC_KEYS
    )
    # A fake cell still pins just fidelity.
    assert expected_metric_keys(FAKE_CELLS["golden-fake"]) == FIDELITY_METRIC_KEYS
    # The matrix cell's keys are dynamic (per-cell labels), so no static list.
    assert expected_metric_keys(FAKE_CELLS["matrix-fake"]) is None


def test_metrics_from_summary_keyed_flattens_answers_and_gaps() -> None:
    spec = KEYED_CELLS["golden-keyed"]
    metrics = metrics_from_summary(spec, _keyed_summary())
    assert set(metrics) == set(KEYED_METRIC_KEYS)
    assert metrics["answer_f1.oracle"] == 0.6
    assert metrics["gaps.total"] == 0.2
    # A fidelity-shaped (fake) cell extracts only the fidelity block.
    fake_metrics = metrics_from_summary(FAKE_CELLS["golden-fake"], _keyed_summary())
    assert set(fake_metrics) == set(FIDELITY_METRIC_KEYS)


# --- F4: --against provenance validation for keyed cells ---------------------------


def test_keyed_summary_problem_rejects_smoke_and_wrong_backend() -> None:
    spec = KEYED_CELLS["golden-keyed"]
    # A valid keyed summary is accepted.
    assert keyed_summary_problem(spec, _keyed_summary()) is None
    # A keyless-smoke summary is refused (stand-in numbers, not an eval).
    smoke = keyed_summary_problem(spec, _keyed_summary(mode="keyless-smoke"))
    assert smoke is not None and "keyless-smoke" in smoke
    # A backend mismatch is refused (not a like-for-like drift).
    wrong = keyed_summary_problem(spec, _keyed_summary(backend="bedrock"))
    assert wrong is not None and "backend" in wrong


def _write_summary(tmp_path: Path, summary: dict[str, Any]) -> Path:
    against = tmp_path / "eval-out"
    against.mkdir()
    (against / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return against


def test_baseline_update_keyed_refuses_smoke_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)
    against = _write_summary(tmp_path, _keyed_summary(mode="keyless-smoke"))

    rc = main(
        [
            "reconstruct",
            "baseline",
            "update",
            "--cell",
            "golden-keyed",
            "--against",
            str(against),
            "--reason",
            "seed",
        ]
    )
    assert rc == 2
    assert "keyless-smoke" in capsys.readouterr().err
    # Nothing was written.
    assert not (baselines / "golden-keyed.json").exists()


def test_baseline_update_keyed_refuses_wrong_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)
    against = _write_summary(tmp_path, _keyed_summary(backend="bedrock"))

    rc = main(
        [
            "reconstruct",
            "baseline",
            "update",
            "--cell",
            "golden-keyed",
            "--against",
            str(against),
            "--reason",
            "seed",
        ]
    )
    assert rc == 2
    assert "backend" in capsys.readouterr().err


def test_baseline_check_keyed_refuses_smoke_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A seeded keyed cell checked against a keyless-smoke summary fails (F4)."""
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)
    seeded = build_cell(
        KEYED_CELLS["golden-keyed"],
        metrics_from_summary(KEYED_CELLS["golden-keyed"], _keyed_summary()),
        "seed",
    )
    (baselines / "golden-keyed.json").write_text(seeded.to_json(), encoding="utf-8")
    against = _write_summary(tmp_path, _keyed_summary(mode="keyless-smoke"))

    rc = main(
        ["reconstruct", "baseline", "check", "--cell", "golden-keyed", "--against", str(against)]
    )
    assert rc == 1
    assert "keyless-smoke" in capsys.readouterr().err


# --- F2: self-enforcement against the CellSpec registry ----------------------------


def test_identity_mismatches_names_each_laundered_field() -> None:
    spec = FAKE_CELLS["golden-fake"]
    keys = list(FIDELITY_METRIC_KEYS)
    good = build_cell(spec, {k: 0.0 for k in keys}, "unit")
    assert identity_mismatches(good, spec, keys) == []
    # Tolerance / mode / backend launders each surface a named mismatch.
    assert any(
        "tolerance" in m
        for m in identity_mismatches(good.model_copy(update={"tolerance": 0.5}), spec, keys)
    )
    assert any(
        "mode" in m
        for m in identity_mismatches(good.model_copy(update={"mode": "warn"}), spec, keys)
    )
    assert any(
        "backend" in m
        for m in identity_mismatches(
            good.model_copy(update={"backend": "anthropic_api"}), spec, keys
        )
    )
    # A deleted metric is caught as a missing-from-file mismatch (no silent shrinkage).
    shrunk = good.model_copy(
        update={"metrics": {k: v for k, v in good.metrics.items() if k != "node_f1"}}
    )
    assert any("missing from file" in m for m in identity_mismatches(shrunk, spec, keys))


def _temp_baselines_with_real_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the real committed fake cells into a temp dir + a fast (file-metric) regen.

    Lets the registry-driven / laundering tests run ``check`` without paying the ~1.2s
    of a real golden+matrix regeneration: the stand-in regenerator returns each cell's
    own committed metrics, so a *clean* copy passes identity + compare and only the
    deliberate perturbation under test fails.
    """
    from enterprise_sim.reconstruct import baseline as baseline_mod

    real = {
        name: cell_path(name).read_text(encoding="utf-8") for name in ("golden-fake", "matrix-fake")
    }
    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    for name, text in real.items():
        (baselines / f"{name}.json").write_text(text, encoding="utf-8")
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)
    monkeypatch.setattr(
        baseline_mod,
        "regenerate_fake_metrics",
        lambda spec: dict(json.loads(real[spec.cell])["metrics"]),
    )
    return baselines


@pytest.mark.parametrize(
    ("update", "needle"),
    [
        ({"tolerance": 0.5}, "tolerance"),
        ({"mode": "warn"}, "mode"),
    ],
)
def test_baseline_check_fails_on_identity_launder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    update: dict[str, Any],
    needle: str,
) -> None:
    """A hand-laundered tolerance/mode in the file fails against the registry (F2)."""
    baselines = _temp_baselines_with_real_fakes(tmp_path, monkeypatch)
    cell = BaselineCell.read(baselines / "golden-fake.json")
    (baselines / "golden-fake.json").write_text(
        cell.model_copy(update=update).to_json(), encoding="utf-8"
    )
    rc = main(["reconstruct", "baseline", "check", "--cell", "golden-fake"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err and needle in err


def test_baseline_check_fails_on_metric_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Deleting a metric from the file fails — no silent shrinkage (F2)."""
    baselines = _temp_baselines_with_real_fakes(tmp_path, monkeypatch)
    cell = BaselineCell.read(baselines / "golden-fake.json")
    shrunk = {k: v for k, v in cell.metrics.items() if k != "node_f1"}
    (baselines / "golden-fake.json").write_text(
        cell.model_copy(update={"metrics": shrunk}).to_json(), encoding="utf-8"
    )
    rc = main(["reconstruct", "baseline", "check", "--cell", "golden-fake"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err and "node_f1" in err


# --- F3: --cell all iterates the registry, not a directory glob --------------------


def test_baseline_check_all_flags_stray_unregistered_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A stray .json not in CELL_SPECS is named + fails, not a raw KeyError (F3)."""
    baselines = _temp_baselines_with_real_fakes(tmp_path, monkeypatch)
    (baselines / "rogue-cell.json").write_text("{}", encoding="utf-8")
    rc = main(["reconstruct", "baseline", "check", "--cell", "all"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unregistered baseline file" in err and "rogue-cell" in err


def test_baseline_check_flags_missing_registered_fake_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A registered fake cell whose committed file vanished is a FAIL (F3)."""
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)
    rc = main(["reconstruct", "baseline", "check", "--cell", "golden-fake"])
    assert rc == 1
    assert "registered baseline cell missing" in capsys.readouterr().err


def test_baseline_check_single_unknown_cell_is_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """An unknown --cell name is a clear message, never a KeyError traceback (F3)."""
    from enterprise_sim.reconstruct import baseline as baseline_mod

    baselines = tmp_path / "evals" / "baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(baseline_mod, "BASELINES_DIR", baselines)
    (baselines / "nope.json").write_text("{}", encoding="utf-8")
    rc = main(["reconstruct", "baseline", "check", "--cell", "nope"])
    assert rc == 1
    assert "unknown cell" in capsys.readouterr().err


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
