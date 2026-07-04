"""Multi-run scale harness + aggregate fidelity tests (esim-ecr.5).

Covers the harness that runs the reconstruction eval across MORE than the single
golden run and aggregates the result, along the axes the acceptance criteria name:

* **Varied run generation** — the default specs are deterministic and actually
  vary the archetype (engineering vs retail), so ≥2 runs are distinct companies.
* **Aggregation** — per-run fidelity reduces to a mean/spread (mean, population
  stdev, min, max) over every scalar metric; the math is exact on hand values.
* **Keyless end to end** — ``run_scale`` generates ≥2 varied runs, reconstructs +
  scores each via the fake backend with no key, and aggregates; the ``reconstruct
  scale`` CLI runs the same path and emits a report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.config import CompanySize
from enterprise_sim.reconstruct import (
    AggregateFidelity,
    RunSpec,
    default_run_specs,
    run_scale,
)
from enterprise_sim.reconstruct.scale import Aggregate, _aggregate, build_aggregate

# --------------------------------------------------------------------------- #
# Run-spec generation.
# --------------------------------------------------------------------------- #


def test_default_specs_are_varied_and_deterministic() -> None:
    specs = default_run_specs(2)
    assert len(specs) == 2
    # The first two specs vary the archetype (engineering vs retail) — the primary
    # axis the epic names — and carry independent, reproducible seeds.
    assert [s.vertical for s in specs] == ["software", "retail"]
    assert [s.seed for s in specs] == [7, 8]
    # Deterministic: same call, same specs.
    assert default_run_specs(2) == specs


def test_default_specs_reject_out_of_range_counts() -> None:
    with pytest.raises(ValueError):
        default_run_specs(0)
    with pytest.raises(ValueError):
        default_run_specs(999)


def test_run_spec_to_config_maps_vertical_and_size() -> None:
    spec = RunSpec(
        label="eng",
        company_name="Eng Co",
        vertical="software",
        size=CompanySize.STARTUP,
        seed=3,
    )
    config = spec.to_config("out")
    assert config.company.vertical == "software"
    assert config.company.size == CompanySize.STARTUP
    assert config.seed == 3
    assert config.output_dir == Path("out")


# --------------------------------------------------------------------------- #
# Aggregation math.
# --------------------------------------------------------------------------- #


def test_aggregate_computes_mean_spread() -> None:
    agg = _aggregate([1.0, 2.0, 3.0])
    assert agg.mean == 2.0
    assert agg.minimum == 1.0
    assert agg.maximum == 3.0
    assert agg.n == 3
    # Population stdev of {1,2,3} is sqrt(2/3).
    assert agg.stdev == pytest.approx((2.0 / 3.0) ** 0.5)


def test_aggregate_single_value_has_zero_spread() -> None:
    agg = _aggregate([0.5])
    assert agg == Aggregate(mean=0.5, stdev=0.0, minimum=0.5, maximum=0.5, n=1)


def test_build_aggregate_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build_aggregate([], backend="fake")


# --------------------------------------------------------------------------- #
# Keyless end to end (fake backend).
# --------------------------------------------------------------------------- #


def _assert_well_formed(aggregate: AggregateFidelity, *, expected_runs: int) -> None:
    assert aggregate.run_count == expected_runs
    assert len(aggregate.runs) == expected_runs
    # Every scalar metric is present with the run count baked into its aggregate.
    for key in ("node_f1", "edge_f1", "node_precision", "gold_nodes"):
        assert key in aggregate.metrics
        assert aggregate.metrics[key].n == expected_runs
    # The gold graphs are non-trivial (the runs really executed).
    for run in aggregate.runs:
        assert run.report.gold_node_count > 0


def test_run_scale_keyless_generates_and_aggregates(tmp_path: Path) -> None:
    aggregate = run_scale(default_run_specs(2), tmp_path)
    _assert_well_formed(aggregate, expected_runs=2)
    # The two runs are distinct companies (different archetype → different gold).
    run_ids = {run.run_id for run in aggregate.runs}
    assert len(run_ids) == 2
    verticals = {run.vertical for run in aggregate.runs}
    assert verticals == {"software", "retail"}


def test_run_scale_is_deterministic(tmp_path: Path) -> None:
    a = run_scale(default_run_specs(2), tmp_path / "a")
    b = run_scale(default_run_specs(2), tmp_path / "b")
    assert a.to_json() == b.to_json()


def test_run_scale_requires_a_spec(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_scale([], tmp_path)


def test_aggregate_renders_markdown_and_json(tmp_path: Path) -> None:
    aggregate = run_scale(default_run_specs(2), tmp_path)
    md = aggregate.to_markdown()
    assert "# Reconstruct scale" in md
    assert "## Per run" in md
    assert "## Aggregate" in md
    # Both varied runs appear as rows.
    for run in aggregate.runs:
        assert run.label in md

    data = aggregate.to_dict()
    assert data["run_count"] == 2
    assert data["backend"] == "fake"
    assert len(data["runs"]) == 2
    assert "node_f1" in data["aggregate"]


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def test_scale_subcommand_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "scale", "-o", "out"])
    assert args.func is not None
    assert args.runs == 2
    assert args.backend == "fake"
    assert args.output == Path("out")


def test_scale_cli_writes_aggregate_report(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "aggregate.md"
    rc = main(
        [
            "reconstruct",
            "scale",
            "--runs",
            "2",
            "--work-dir",
            str(tmp_path / "runs"),
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    report = out.read_text(encoding="utf-8")
    assert "# Reconstruct scale" in report
    assert "## Aggregate" in report
    # A one-line summary goes to stderr.
    err = capsys.readouterr().err
    assert "reconstruct scale" in err
    assert "2 runs" in err


def test_scale_cli_json(tmp_path: Path, capsys: Any) -> None:
    rc = main(
        [
            "reconstruct",
            "scale",
            "--runs",
            "2",
            "--work-dir",
            str(tmp_path / "runs"),
            "--json",
        ]
    )
    assert rc == 0
    import json

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["run_count"] == 2
    assert data["backend"] == "fake"
