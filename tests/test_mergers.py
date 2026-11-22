"""``[org] merger_days``: an acquired company joins on a dated day; its system of record moves."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


def _build(root: Path, days: int) -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / "analytics-structured-clean.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = re.sub(r"(?ms)^\[org\]\n(?:(?!^\[).)*", "", text)
    text += f'\n[org]\nmerger_days = {days}\nmerger_department = "sales"\n'
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world_dir,) = list((root / "w").iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("merger") / "a", 40)


def test_the_merger_is_dated_and_labelled_with_one_company(world: Path) -> None:
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    edges = _jsonl(world / "kg" / "edges.jsonl")
    assert len([n for n in nodes.values() if n["type"] == "Company"]) == 1  # importers refuse two
    (acquired,) = [n for n in nodes.values() if n["type"] == "Acquisition"]
    (edge,) = [e for e in edges if e["type"] == "acquired"]
    assert nodes[edge["src"]]["type"] == "Company" and edge["dst"] == acquired["id"]
    assert edge["created_at"] == acquired["props"]["acquired_at"] == "2026-02-14T09:00:00"
    (dept,) = [
        n for n in nodes.values() if n["type"] == "Department" and n["props"].get("merged_at")
    ]
    assert dept["props"]["archetype"] == "sales"
    assert dept["props"]["acquired_from"] == acquired["props"]["name"]


def test_the_system_of_record_moves_on_the_merger_day(world: Path) -> None:
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    (acquired,) = [n for n in nodes.values() if n["type"] == "Acquisition"]
    (company,) = [n for n in nodes.values() if n["type"] == "Company"]
    histories: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "FactVersion" and node["props"]["fact"].endswith(":system_of_record"):
            histories.setdefault(node["props"]["fact"], []).append(node["props"])
    assert histories
    for history in histories.values():
        first, second = sorted(history, key=lambda v: v["version"])
        assert first["value"] == f"{acquired['props']['name']} CRM"
        assert second["value"] == f"{company['props']['name']} CRM"
        assert first["valid_to"] == second["valid_from"] == acquired["props"]["acquired_at"]


def test_documents_state_the_system_true_when_written(world: Path) -> None:
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    stances = []
    for edge in _jsonl(world / "kg" / "edges.jsonl"):
        if edge["type"] != "asserts" or not edge["dst"].endswith(":system_of_record"):
            continue
        path = world / str(nodes[edge["src"]]["props"]["path"])
        if path.suffix == ".md":
            assert edge["props"]["value"] in path.read_text(encoding="utf-8"), edge
        stances.append(edge["props"]["stance"])
    assert "update" in stances and "current" in stances


@pytest.mark.parametrize("days", [0, 400])
def test_no_merger_when_off_or_beyond_the_window(tmp_path: Path, days: int) -> None:
    world = _build(tmp_path / f"d{days}", days)
    assert not [n for n in _jsonl(world / "kg" / "nodes.jsonl") if n["type"] == "Acquisition"]
