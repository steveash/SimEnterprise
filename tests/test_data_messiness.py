"""``[data.messiness]``: dirty raw tables, clean views, every change in quirks.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.core.config.diversity import DataMessinessConfig


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], messiness: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 5\n"
        '[company]\nname = "Data Co"\nvertical = "analytics"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-27\n"
        '[data]\nscenarios = ["support_ops"]\nscale_factor = 0.1\niterations = 1\n'
        "rows_per_partition = 50000\n" + messiness,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def _rows(directory: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for part in sorted(directory.glob("*.parquet")):
        rows.extend(pq.read_table(part).to_pylist())
    return rows


def test_default_is_clean() -> None:
    assert DataMessinessConfig().clean
    assert not DataMessinessConfig(null_rate=0.1).clean


def test_messy_tables_carry_their_quirks_and_views_stay_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_dir = _build(tmp_path / "a", capsys, "")
    messy_dir = _build(
        tmp_path / "b",
        capsys,
        "[data.messiness]\nnull_rate = 0.05\nduplicate_rate = 0.05\nlabel_drift_rate = 0.2\n"
        "outlier_rate = 0.02\nschema_drift = true\n",
    )
    clean = clean_dir / "data" / "support_ops"
    messy = messy_dir / "data" / "support_ops"
    assert not (clean / "quirks.json").exists()
    report = json.loads((messy / "quirks.json").read_text(encoding="utf-8"))
    assert {"null", "duplicate_row", "label_drift", "outlier", "schema_drift"} <= set(
        report["counts"]
    )
    # Views are untouched: byte-identical to the clean world's.
    for view in sorted((clean / "views").iterdir()):
        assert _rows(view) == _rows(messy / "views" / view.name)
    # Raw tables: duplicates add rows, nulls appear, drift renames a column.
    for table, quirks in report["tables"].items():
        before = _rows(clean / "tables" / table)
        after = _rows(messy / "tables" / table)
        dupes = sum(1 for q in quirks if q["kind"] == "duplicate_row")
        assert len(after) == len(before) + dupes
        drift = [q for q in quirks if q["kind"] == "schema_drift"]
        if drift:
            parts = sorted((messy / "tables" / table).glob("*.parquet"))
            assert len(parts) == 2
            column = drift[0]["column"]
            assert any(f"{column}_v2" in row for row in after)
        nulls = [q for q in quirks if q["kind"] == "null"]
        if nulls:
            assert sum(1 for row in after for v in row.values() if v is None) >= len(nulls)


def test_a_messy_world_exports_with_the_union_of_drifted_columns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from enterprise_sim.exporters.text_export import export_world

    world_dir = _build(
        tmp_path, capsys, "[data.messiness]\nschema_drift = true\nnull_rate = 0.05\n"
    )
    result = export_world(world_dir, tmp_path / "out")
    manifest = json.loads((result.directory / "export.json").read_text(encoding="utf-8"))
    files = {f["path"]: f for f in manifest["files"]}
    assert files["data/support_ops/quirks.json"]["role"] == "gold"
    drifted = [
        t
        for t in manifest["tables"]
        if t["kind"] == "table" and any(c["name"].endswith("_v2") for c in t["columns"])
    ]
    assert drifted
