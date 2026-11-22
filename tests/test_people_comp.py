"""``people_comp``: per-person Q4 bonuses on the world's real people, reconciled pools."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("people_comp")
    text = (_EXAMPLES / "docs-only-professional.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text += '\n[data]\nscenarios = ["people_comp"]\nscale_factor = 1.0\niterations = 1\n'
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return world, world / "data" / "people_comp"


def _rows(directory: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return [r for p in sorted(directory.glob("*.parquet")) for r in pq.read_table(p).to_pylist()]


def test_every_row_is_a_real_person_with_their_department(data: tuple[Path, Path]) -> None:
    world, scenario = data
    nodes = {
        json.loads(line)["id"]: json.loads(line)
        for line in (world / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
    }
    rows = _rows(scenario / "tables" / "fact_employee_bonus")
    people = [n for n in nodes.values() if n["type"] == "Person"]
    assert len(rows) == len(people)
    for row in rows:
        person = nodes[row["person_id"]]["props"]
        assert row["department"] == person["department"]
        assert row["cost_centre"] == person["unit"]
        assert row["seniority"] == person["seniority"]


def test_amounts_have_two_decimals_and_non_zero_cents(data: tuple[Path, Path]) -> None:
    _, scenario = data
    amounts = [r["q4_bonus"] for r in _rows(scenario / "tables" / "fact_employee_bonus")]
    amounts += [r["amount"] for r in _rows(scenario / "tables" / "fact_bonus_adjustment")]
    for amount in amounts:
        cents = round(amount * 100)
        assert abs(amount * 100 - cents) < 1e-6, amount  # two decimals
        assert cents % 10 != 0, amount  # the last cent digit is never 0


def test_exactly_one_retention_payment_due_in_january(data: tuple[Path, Path]) -> None:
    _, scenario = data
    (adjustment,) = _rows(scenario / "tables" / "fact_bonus_adjustment")
    assert adjustment["kind"] == "retention"
    assert re.fullmatch(r"\d{4}-01-31", adjustment["due_date"])
    names = {r["name"] for r in _rows(scenario / "tables" / "fact_employee_bonus")}
    assert adjustment["employee"] in names


def test_department_totals_and_cost_centre_pools_reconcile(data: tuple[Path, Path]) -> None:
    _, scenario = data
    rows = _rows(scenario / "tables" / "fact_employee_bonus")
    (adjustment,) = _rows(scenario / "tables" / "fact_bonus_adjustment")
    by_department: dict[str, float] = defaultdict(float)
    by_centre: dict[str, float] = defaultdict(float)
    centre_of = {r["name"]: r["cost_centre"] for r in rows}
    for row in rows:
        by_department[row["department"]] += row["q4_bonus"]
        by_centre[row["cost_centre"]] += row["q4_bonus"]
    by_centre[centre_of[adjustment["employee"]]] += adjustment["amount"]
    for row in _rows(scenario / "views" / "bonus_by_department"):
        assert row["total_bonus"] == pytest.approx(round(by_department[row["department"]], 2))
    pools = _rows(scenario / "views" / "bonus_pool_by_cost_centre")
    assert {r["cost_centre"] for r in pools} == set(by_centre)
    for row in pools:
        assert row["gross_pool"] == pytest.approx(round(by_centre[row["cost_centre"]], 2))
        assert row["gross_pool"] == pytest.approx(row["bonus"] + row["adjustment"])


def test_new_spec_fields_leave_existing_scenarios_unchanged() -> None:
    from datetime import date

    from enterprise_sim.data_products.scenarios import discover_scenarios

    registry = discover_scenarios()
    for name in ("support_ops", "sales_pipeline", "subscription_finance", "pipeline_ops"):
        spec = registry.get(name).build_spec(start=date(2026, 1, 5), end=date(2026, 3, 27))
        text = json.dumps(spec.model_dump(mode="json", exclude_none=True))
        assert '"props"' not in text and '"decimals"' not in text, name
