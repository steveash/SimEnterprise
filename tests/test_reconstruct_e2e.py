"""Keyless coverage for the one-command attribution eval (spec 0003 slice 1).

``enterprise-sim reconstruct e2e`` drives the whole chain — build → fidelity →
reason → report — in-process. The keyed reason slots need a key, so these tests
exercise the ``--keyless-smoke`` path: it forces the ``fake`` backend and stands
one keyless RAG prediction in for all three slots, so the full plumbing runs end
to end with no key. We assert the wiring (every artifact lands, ``summary.json``
parses and is deterministic), not the stand-in numbers, plus the CLI arg surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import build_parser, main
from enterprise_sim.reconstruct.e2e import (
    ATTRIBUTION_FILE,
    BENCH_FILE,
    DEFAULT_REASON_MODEL,
    FIDELITY_FILE,
    PRED_ORACLE_FILE,
    PRED_RAG_FILE,
    PRED_RECONSTRUCTED_FILE,
    SUMMARY_FILE,
    _attribution_markdown,
    run_e2e,
)

# Every artifact a keyless-smoke run must land under --out (mirrors the shell
# harness's assertions, plus this module's summary.json addition).
_EXPECTED_FILES = (
    BENCH_FILE,
    FIDELITY_FILE,
    PRED_ORACLE_FILE,
    PRED_RECONSTRUCTED_FILE,
    PRED_RAG_FILE,
    ATTRIBUTION_FILE,
    SUMMARY_FILE,
)


def test_keyless_smoke_writes_every_artifact(tmp_path: Path) -> None:
    result = run_e2e(tmp_path / "eval", keyless_smoke=True)
    out = result.out_dir

    for name in _EXPECTED_FILES:
        assert (out / name).is_file(), f"missing {name}"
    assert (out / "recon" / "nodes.jsonl").is_file()

    # Smoke mode forces the fake backend and flags itself as such.
    assert result.mode == "keyless-smoke"
    assert result.backend == "fake"
    # The gold run is the pinned golden run (reused, not a second pinned config).
    assert result.run_id == "golden-slice-co-6c66fbef69f8"

    # REPORT rendered the three-system attribution table.
    report = (out / ATTRIBUTION_FILE).read_text(encoding="utf-8")
    assert "Reconstruct attribution report" in report
    assert "oracle" in report and "reconstructed" in report and "rag" in report


def test_summary_parses_and_flags_smoke_mode(tmp_path: Path) -> None:
    result = run_e2e(tmp_path / "eval", keyless_smoke=True)
    summary = json.loads((result.out_dir / SUMMARY_FILE).read_text(encoding="utf-8"))

    assert summary["mode"] == "keyless-smoke"
    assert summary["backend"] == "fake"
    assert summary["run_id"] == "golden-slice-co-6c66fbef69f8"
    # The loud stand-ins note is carried in the summary too.
    assert "wiring stand-ins" in summary["note"]
    # The fidelity block carries the exact metric key set the baseline cells pin.
    assert set(summary["fidelity"]) == {
        "node_f1",
        "node_precision",
        "node_recall",
        "edge_f1",
        "edge_precision",
        "edge_recall",
        "provenance_f1",
        "over_merges",
        "under_merges",
        "reconstructed_nodes",
        "reconstructed_edges",
    }
    assert set(summary["answer_f1"]) == {"oracle", "reconstructed", "rag"}
    assert set(summary["gaps"]) == {"understanding", "reasoning", "total"}
    # The returned E2EResult's summary matches the file byte-for-byte.
    assert result.summary == summary


def test_summary_is_deterministic(tmp_path: Path) -> None:
    """Two keyless-smoke runs produce byte-identical summary.json (D10/D31)."""
    first = run_e2e(tmp_path / "a", keyless_smoke=True)
    second = run_e2e(tmp_path / "b", keyless_smoke=True)
    assert (first.out_dir / SUMMARY_FILE).read_text(encoding="utf-8") == (
        second.out_dir / SUMMARY_FILE
    ).read_text(encoding="utf-8")


def test_e2e_subcommand_arg_surface() -> None:
    """The CLI parser exposes the flags parity requires, with the spec's defaults."""
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "e2e", "-o", "out", "--keyless-smoke"])
    assert args.func is not None
    assert args.output == Path("out")
    assert args.keyless_smoke is True
    # Keyed-first defaults (the keyless mode is the explicit flag), matching the
    # shell harness's semantics.
    assert args.backend == "anthropic_api"
    assert args.model == DEFAULT_REASON_MODEL
    assert args.run is None
    assert args.limit is None
    assert args.use_bedrock is False
    assert args.aws_region is None
    # --out is an accepted alias for -o.
    aliased = parser.parse_args(["reconstruct", "e2e", "--out", "elsewhere"])
    assert aliased.output == Path("elsewhere")


def test_e2e_cli_keyless_smoke_writes_summary(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "eval"
    rc = main(["reconstruct", "e2e", "--keyless-smoke", "-o", str(out)])
    assert rc == 0

    summary = json.loads((out / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["mode"] == "keyless-smoke"

    err = capsys.readouterr().err
    # The one-line stderr summary + the loud stand-ins note both land.
    assert "reconstruct e2e: mode=keyless-smoke" in err
    assert "wiring stand-ins" in err


def test_smoke_banner_marks_the_attribution_report(tmp_path: Path) -> None:
    """In smoke mode the written report leads with the loud stand-ins banner (F5).

    The banner is applied at the write site, not in ``render_markdown``, so the same
    attribution rendered for the keyed path carries no banner (unit level).
    """
    result = run_e2e(tmp_path / "eval", keyless_smoke=True)
    report = (result.out_dir / ATTRIBUTION_FILE).read_text(encoding="utf-8")
    assert report.startswith("> **KEYLESS SMOKE**")
    assert "NOT an eval" in report.splitlines()[0]
    # The keyed path construction renders the exact same attribution unbannered.
    keyed = _attribution_markdown(result.attribution, keyless_smoke=False)
    assert not keyed.startswith("> **KEYLESS SMOKE**")
    assert "KEYLESS SMOKE" not in keyed


@pytest.mark.parametrize("backend", ["anthropic_api", "bedrock"])
def test_keyless_smoke_forces_fake_over_any_backend(tmp_path: Path, backend: str) -> None:
    """--keyless-smoke overrides the backend so it never needs a key/creds."""
    result = run_e2e(tmp_path / backend, backend=backend, keyless_smoke=True)
    assert result.backend == "fake"
    assert result.summary["backend"] == "fake"
