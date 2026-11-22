"""``[data.messiness] recipe`` and ``null_policy``: per-table recipes, recoverable nulls."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.core.config.diversity import DataMessinessConfig, NullPolicy, QuirkRecipe

_MESSY = (
    "[data.messiness]\nnull_rate = 0.08\nduplicate_rate = 0.03\nlabel_drift_rate = 0.1\n"
    "outlier_rate = 0.05\nschema_drift = true\n"
)


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 5\n"
        '[company]\nname = "Data Co"\nvertical = "analytics"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-27\n"
        '[data]\nscenarios = ["support_ops", "subscription_finance"]\nscale_factor = 0.1\n'
        "iterations = 1\nrows_per_partition = 50000\n" + _MESSY + extra,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir / "data"


def _rows(directory: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for part in sorted(directory.glob("*.parquet")):
        rows.extend(pq.read_table(part).to_pylist())
    return rows


def _report(data: Path, scenario: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads((data / scenario / "quirks.json").read_text("utf-8"))
    return out


def test_later_knobs_stay_out_of_quirks_json_at_their_defaults() -> None:
    cfg = DataMessinessConfig(null_rate=0.1)
    assert "recipe" not in cfg.stated() and "null_policy" not in cfg.stated()
    varied = DataMessinessConfig(
        null_rate=0.1, recipe=QuirkRecipe.VARIED, null_policy=NullPolicy.CARRY_FORWARD
    )
    assert varied.stated()["recipe"] == "varied"
    assert varied.stated()["null_policy"] == "carry_forward"
    assert DataMessinessConfig(recipe=QuirkRecipe.VARIED).clean  # a recipe alone dirties nothing


def test_a_fixed_recipe_declares_no_recipes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _build(tmp_path, capsys, "")
    for scenario in ("support_ops", "subscription_finance"):
        report = _report(data, scenario)
        assert "recipes" not in report
        for quirks in report["tables"].values():
            for q in quirks:
                if q["kind"] == "schema_drift":
                    assert q["surface"] == q["column"] + "_v2"
                    assert q["row"] == {next(iter(q["row"])): "2026-01-31"}


def test_a_varied_recipe_draws_per_table_and_states_what_it_drew(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _build(tmp_path, capsys, 'recipe = "varied"\n')
    drawn: list[tuple[Any, ...]] = []
    for scenario in ("support_ops", "subscription_finance"):
        report = _report(data, scenario)
        assert report["config"]["recipe"] == "varied"
        assert set(report["recipes"]) == set(report["tables"])
        for table, quirks in report["tables"].items():
            recipe = report["recipes"][table]
            drawn.append(tuple(sorted(recipe.items())))
            if not any(q["kind"] == "schema_drift" for q in quirks):
                assert recipe["drift_day"] is None and recipe["drift_name"] is None, table
            else:
                day = date.fromisoformat(recipe["drift_day"])
                assert date(2026, 1, 5) < day < date(2026, 2, 27)
            for q in quirks:
                if q["kind"] == "outlier":
                    original, surface = q["original"], q["surface"]
                    assert surface in (
                        pytest.approx(original * recipe["unit_factor"]),
                        pytest.approx(-original),
                    ), (table, q)
                if q["kind"] == "schema_drift":
                    renamed = recipe["drift_name"].replace("<col>", q["column"])
                    assert q["surface"] == renamed and q["row"][next(iter(q["row"]))] == str(day)
                    after = _rows(data / scenario / "tables" / table)
                    assert any(renamed in row for row in after)
    assert len(set(drawn)) > 1, "tables must not all share one recipe"


def _sources(data: Path, scenario: str, table: str) -> dict[str, str]:
    """Column name -> its spec source (entity_id, date, attribute, panel, ...)."""
    spec = json.loads((data / scenario / "spec.json").read_text("utf-8"))
    (found,) = [t for t in spec["tables"] if t["name"] == table]
    return {c["name"]: c["source"] for c in found["columns"]}


def _named(sources: dict[str, str], source: str) -> str:
    (name,) = [n for n, s in sources.items() if s == source]
    return name


def test_attribute_nulls_leave_every_measure_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _build(tmp_path, capsys, 'null_policy = "attributes"\n')
    nulled = 0
    for scenario in ("support_ops", "subscription_finance"):
        report = _report(data, scenario)
        for table, quirks in report["tables"].items():
            sources = _sources(data, scenario, table)
            nulls = [q for q in quirks if q["kind"] == "null"]
            assert sorted({q["column"] for q in nulls}) == report["recipes"][table]["null_columns"]
            assert "attribute" in report["recipes"][table]["null_rule"]
            assert all(sources[q["column"]] == "attribute" for q in nulls), table
            nulled += len(nulls)
            measures = [n for n, src in sources.items() if src == "panel"]
            for row in _rows(data / scenario / "tables" / table):
                assert all(row.get(m) is not None for m in measures if m in row), table
    assert nulled > 0


def test_carry_forward_nulls_are_recovered_by_filling_forward(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _build(tmp_path, capsys, 'null_policy = "carry_forward"\n')
    recovered = 0
    for scenario in ("support_ops", "subscription_finance"):
        report = _report(data, scenario)
        for table, quirks in report["tables"].items():
            sources = _sources(data, scenario, table)
            nulls = [q for q in quirks if q["kind"] == "null"]
            if not nulls:
                continue
            entity, day = _named(sources, "entity_id"), _named(sources, "date")
            outliers = {
                (q["row"][entity], q["row"][day], q["column"]): q["original"]
                for q in quirks
                if q["kind"] == "outlier"
            }
            rows = sorted(
                {
                    (r[entity], str(r[day])): r for r in _rows(data / scenario / "tables" / table)
                }.values(),
                key=lambda r: (str(r[entity]), str(r[day])),
            )
            last: dict[tuple[Any, str], Any] = {}
            filled: dict[tuple[Any, str, str], Any] = {}
            for row in rows:
                for name, value in row.items():
                    column = name
                    for q in quirks:  # a drifted column fills from its old name
                        if q["kind"] == "schema_drift" and q["surface"] == name:
                            column = q["column"]
                    key = (row[entity], str(row[day]), column)
                    if value is None:
                        filled[key] = last.get((row[entity], column))
                    else:
                        last[(row[entity], column)] = outliers.get(key, value)
            for q in nulls:
                got = filled[(q["row"][entity], q["row"][day], q["column"])]
                assert got == pytest.approx(q["original"]) or got == q["original"], (table, q)
                recovered += 1
    assert recovered > 0
