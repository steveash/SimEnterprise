"""A department's own ``squads_per_team`` staffs that department alone."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from enterprise_sim.cli import main

_LADDER = Path(__file__).resolve().parents[1] / "examples" / "worlds" / "ladder"


def _people(root: Path, extra: str) -> dict[str, dict[str, str]]:
    """Build ladder-s with ``extra`` departments appended; person id -> (name, department)."""
    root.mkdir(parents=True)
    text = (_LADDER / "ladder-s.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = text.replace("\n[facts]", "\n" + extra + "\n[facts]", 1)
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    nodes = [json.loads(line) for line in (world / "kg" / "nodes.jsonl").read_text().splitlines()]
    return {
        n["id"]: {"name": n["props"]["name"], "department": n["props"].get("department", "")}
        for n in nodes
        if n["type"] == "Person"
    }


def test_more_squads_in_one_department_leave_the_others_unchanged(tmp_path: Path) -> None:
    one = _people(tmp_path / "one", '[[departments]]\narchetype = "finance"\n')
    three = _people(
        tmp_path / "three",
        '[[departments]]\narchetype = "finance"\nsquads_per_team = "3..3"\n',
    )

    def of(people: dict[str, dict[str, str]], department: str) -> dict[str, dict[str, str]]:
        return {i: p for i, p in people.items() if p["department"] == department}

    assert len(of(three, "finance")) >= 2 * len(of(one, "finance"))
    for department in ("data_analytics", "marketing", "sales"):
        assert of(three, department) == of(one, department), department


def test_an_unset_department_knob_is_not_in_the_stored_config() -> None:
    from enterprise_sim.assembly import canonical_config
    from enterprise_sim.core.config import load_config

    def departments(config: Path) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], canonical_config(load_config(config))["departments"])

    stored = departments(_LADDER / "ladder-s.toml")
    assert stored and all(set(d) == {"archetype"} for d in stored)
    tiered = departments(_LADDER / "tiered" / "ladder-l.toml")
    assert {d.get("squads_per_team") for d in tiered} == {None, "3..3"}


def test_a_department_knob_must_start_at_one() -> None:
    from enterprise_sim.core.config.models import DepartmentConfig

    with pytest.raises(ValueError):
        DepartmentConfig.model_validate({"archetype": "finance", "squads_per_team": "0..2"})
    assert DepartmentConfig(archetype="finance", squads_per_team="2..3").squads_range == (2, 3)
