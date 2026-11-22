"""``[org] reorgs``: projects move department on a dated day; the reporting line follows."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


def _build(root: Path, reorgs: int, end: str | None = None) -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / "analytics-structured-clean.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = re.sub(r"(?ms)^\[org\]\n(?:(?!^\[).)*", "", text)
    if end is not None:
        text = re.sub(r"(?m)^period_end = .*$", f"period_end = {end}", text)
    text += f"\n[org]\nreorgs = {reorgs}\nhorizon_days = 80\n"
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world_dir,) = list((root / "w").iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("reorgs") / "a", 2)


def _moved(world: Path) -> dict[str, dict[str, Any]]:
    return {
        n["id"]: n
        for n in _jsonl(world / "kg" / "nodes.jsonl")
        if n["type"] == "Project" and n["props"].get("reorg_at")
    }


def test_reorgs_are_dated_and_labelled(world: Path) -> None:
    moved = _moved(world)
    assert moved
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    edges = [e for e in _jsonl(world / "kg" / "edges.jsonl") if e["type"] == "reorganized_into"]
    for project_id, project in moved.items():
        props = project["props"]
        assert props["reorg_from"] == props["department"] != props["reorg_to"]
        (edge,) = [e for e in edges if e["src"] == project_id]
        assert nodes[edge["dst"]]["props"]["archetype"] == props["reorg_to"]
        assert edge["created_at"] == props["reorg_at"]
        assert not props.get("walled")


def test_the_reporting_line_changes_on_the_reorg_day(world: Path) -> None:
    moved = _moved(world)
    versions: dict[str, list[dict[str, Any]]] = {}
    for node in _jsonl(world / "kg" / "nodes.jsonl"):
        if node["type"] == "FactVersion" and node["props"]["fact"].endswith(":reporting_line"):
            versions.setdefault(node["props"]["fact"], []).append(node["props"])
    assert len(versions) == len(moved)
    for project in moved.values():
        slug = project["id"].split(":", 1)[-1].replace("_", "-")
        (history,) = [v for f, v in versions.items() if slug in f]
        first, second = sorted(history, key=lambda v: v["version"])
        assert first["valid_to"] == second["valid_from"] == project["props"]["reorg_at"]
        from_display = project["props"]["reorg_from"].replace("_", " ").title()
        to_display = project["props"]["reorg_to"].replace("_", " ").title()
        assert first["value"].startswith(from_display) and second["value"].startswith(to_display)


def test_documents_state_the_line_true_when_written(world: Path) -> None:
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    edges = _jsonl(world / "kg" / "edges.jsonl")
    stances = []
    for edge in edges:
        if edge["type"] != "asserts" or not edge["dst"].endswith(":reporting_line"):
            continue
        artifact = nodes[edge["src"]]
        path = world / str(artifact["props"]["path"])
        if path.suffix == ".md":
            assert edge["props"]["value"] in path.read_text(encoding="utf-8"), edge
        if edge["props"]["stance"] in ("current", "hedged", "update"):
            assert edge["props"]["stated_version"] == edge["props"]["true_version"], edge
        stances.append(edge["props"]["stance"])
    assert "update" in stances and "current" in stances


def test_extending_the_window_never_moves_a_past_reorg(world: Path, tmp_path: Path) -> None:
    longer = _build(tmp_path / "longer", 2, end="2026-04-24")
    before = {p: n["props"]["reorg_at"] for p, n in _moved(world).items()}
    after = {p: n["props"]["reorg_at"] for p, n in _moved(longer).items()}
    for project, at in before.items():
        assert after.get(project) == at
    assert all(datetime.fromisoformat(a) < datetime(2026, 4, 24) for a in after.values())


def test_no_reorgs_by_default(tmp_path: Path) -> None:
    assert not _moved(_build(tmp_path / "off", 0))
