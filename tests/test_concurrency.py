"""Concurrent days: a cross-team fact found mid-day, the needing team's work before and after it."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


def _world(root: Path, extra: str) -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / "ladder" / "ladder-s.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?ms)^\[ladder\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = re.sub(r"(?m)^cross_team = .*$", "cross_team = 1.0", text)
    (root / "cfg.toml").write_text(text + extra, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return world


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    world = _world(
        tmp_path_factory.mktemp("concurrency") / "a",
        "\n[concurrency]\ndays = 2\n\n[access]\nrestricted_projects = 1\n",
    )
    out = tmp_path_factory.mktemp("concurrency-export")
    return export_world(world, out, with_access=True, with_knowledge=True).directory


def _graph(export: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    nodes = {
        n["id"]: n
        for n in map(json.loads, (export / "kg" / "nodes.jsonl").read_text().splitlines())
    }
    edges = [json.loads(line) for line in (export / "kg" / "edges.jsonl").read_text().splitlines()]
    return nodes, edges


def test_each_day_finds_a_fact_mid_day_and_the_other_team_states_the_old_value(
    export: Path,
) -> None:
    nodes, edges = _graph(export)
    days = [n for n in nodes.values() if n["type"] == "ConcurrentDay"]
    assert days
    artifact_of = {n["props"]["event"]: i for i, n in nodes.items() if n["type"] == "Artifact"}
    stance_of = {
        (e["src"], e["dst"]): (e["props"]["stance"], e["props"]["stated_version"])
        for e in edges
        if e["type"] == "asserts"
    }
    for day in days:
        p = day["props"]
        found = datetime.fromisoformat(p["discovered_at"])
        assert 11 <= found.hour <= 14  # mid-day, not the day's start
        fv = nodes[f"{p['fact']}@v{p['version']}"]["props"]
        assert fv["valid_from"] == p["discovered_at"]
        prev = nodes[f"{p['fact']}@v{p['version'] - 1}"]["props"]
        assert prev["valid_to"] == p["discovered_at"]
        times = {}
        for kind, expected in (
            ("discovery", ("update", p["version"])),
            ("before", ("current", p["version"] - 1)),
            ("after", ("stale", p["version"] - 1)),
        ):
            aid = artifact_of[p["events"][kind]]
            assert stance_of[(aid, p["fact"])] == expected, kind
            times[kind] = datetime.fromisoformat(nodes[aid]["created_at"])
        assert times["before"] < times["discovery"] < times["after"]
        assert times["after"] - times["before"] == timedelta(hours=4)
        assert nodes[artifact_of[p["events"]["discovery"]]]["props"]  # rendered


def test_the_knowledge_layer_exports_the_workstreams(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE, check_layer

    layer = json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    assert check_layer(export, layer) == []
    streams = layer["workstreams"]
    nodes, _ = _graph(export)
    assert len(streams) == sum(1 for n in nodes.values() if n["type"] == "ConcurrentDay")
    items = {i["id"]: i for i in layer["items"]}
    for s in streams:
        item = items[s["item"]]
        assert s["answer"]["value"] == item["value"]
        assert s["needed_by"]["after"]["stated_item"] == item["supersedes"]
        assert s["needed_by"]["before"]["stated_item"] == item["supersedes"]
        assert s["discovery"]["stated_item"] == s["item"]
        assert "everyone" in s["discovery"]  # the export carries access.json
        assert s["owner_team"]["project"] != s["needed_by"]["project"]
        assert set(s["needed_by"]["people"]) and s["discoverer"] in s["owner_team"]["people"]
        for kind in ("before", "after"):
            assert s["needed_by"][kind]["artifact"] in nodes
        for extra in s["costly_facts"]:  # a second fact, held in a table the publisher reads
            other = items[extra["item"]]
            assert other["subject"]["id"] == s["needed_by"]["project"]
            assert extra["tables"] and all(
                extra["canonical"] in (export / t).read_text(encoding="utf-8")
                for t in extra["tables"]
            )
    edited = json.loads(json.dumps(layer))
    edited["workstreams"][0]["discovered_at"] = "2026-01-01T00:00:00"
    assert any("workstreams" in p for p in check_layer(export, edited))


def test_without_concurrent_days_nothing_changes(tmp_path: Path) -> None:
    from enterprise_sim.assembly import canonical_config
    from enterprise_sim.core.config import load_config
    from enterprise_sim.worlds.knowledge import build_layer

    assert "concurrency" not in canonical_config(
        load_config(_EXAMPLES / "ladder" / "ladder-s.toml")
    )
    world = _world(tmp_path / "p", "")
    out = export_world(world, tmp_path / "out").directory
    nodes, _ = _graph(out)
    assert not any(n["type"] == "ConcurrentDay" for n in nodes.values())
    assert "workstreams" not in build_layer(out)
