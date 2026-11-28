"""Every planted document names its project, so the reader lists restrict it like the rest.

The direction that must never fail is too wide (decision record 0130): a planted
document of a restricted or walled project read by everyone. The planters
(``facts.concurrency``) enter the world through one corpus function;
this test runs over every kind of planted event, and over every document of the world.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.access import ACCESS_FILE, restricted_projects
from enterprise_sim.exporters.text_export import export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"

#: Every planter's event id prefix and the event types it plants. A new planter adds a
#: row here, or the census below fails on its documents.
_PLANTERS = {
    "evt:concurrency:": {"ConcurrentFinding", "ConcurrentWork"},
}


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("planted") / "a"
    root.mkdir()
    text = (_EXAMPLES / "ladder" / "ladder-s.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?ms)^\[ladder\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = re.sub(r"(?m)^cross_team = .*$", "cross_team = 1.0", text)
    extra = "\n[concurrency]\ndays = 3\n\n[access]\nrestricted_projects = 2\n"
    (root / "cfg.toml").write_text(text + extra, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    out = tmp_path_factory.mktemp("planted-export")
    return export_world(world, out, with_access=True, with_knowledge=True).directory


def _graph(export: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    nodes = {
        n["id"]: n
        for n in map(json.loads, (export / "kg" / "nodes.jsonl").read_text().splitlines())
    }
    edges = [json.loads(line) for line in (export / "kg" / "edges.jsonl").read_text().splitlines()]
    return nodes, edges


def _planted(nodes: dict[str, Any]) -> dict[str, str]:
    """Planted artifact id -> its planter prefix."""
    out: dict[str, str] = {}
    for aid, n in nodes.items():
        if n["type"] != "Artifact":
            continue
        event = str(n["props"].get("event", ""))
        for prefix in _PLANTERS:
            if event.startswith(prefix):
                out[aid] = prefix
    return out


def test_every_document_of_the_world_names_its_project(export: Path) -> None:
    nodes, edges = _graph(export)
    named = {e["src"] for e in edges if e["type"] == "expresses"}
    artifacts = [a for a, n in nodes.items() if n["type"] == "Artifact"]
    assert artifacts and all(a in named for a in artifacts), [
        a for a in artifacts if a not in named
    ][:5]


@pytest.mark.parametrize("prefix", sorted(_PLANTERS))
def test_each_planter_plants_documents_of_its_project(export: Path, prefix: str) -> None:
    nodes, edges = _graph(export)
    planted = [a for a, p in _planted(nodes).items() if p == prefix]
    assert planted, prefix  # the fixture exercises every planter
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    for aid in planted:
        assert nodes[project_of[aid]]["type"] == "Project", aid


@pytest.mark.parametrize("prefix", sorted(_PLANTERS))
def test_a_planted_document_of_a_restricted_project_gets_its_reader_list(
    export: Path, prefix: str
) -> None:
    nodes, edges = _graph(export)
    config = json.loads((export / "config.json").read_text(encoding="utf-8"))
    restricted = restricted_projects(nodes, config) | {
        p for p, n in nodes.items() if n["type"] == "Project" and n["props"].get("walled")
    }
    staff: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "member_of" and nodes.get(e["dst"], {}).get("type") == "Project":
            staff.setdefault(e["dst"], set()).add(e["src"])
    authors: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "authored":
            authors.setdefault(e["dst"], set()).add(e["src"])
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    rows = json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))["documents"]
    listed = {r["artifact"]: r for r in rows if r["artifact"] is not None}
    probed = 0
    for aid, p in _planted(nodes).items():
        if p != prefix or project_of.get(aid) not in restricted:
            continue
        probed += 1
        project = project_of[aid]
        assert aid in listed, aid  # restricted, never everyone's
        readers = set(listed[aid]["readers"])
        assert readers <= staff[project] | authors.get(aid, set()), aid
    assert probed, prefix  # some planted document of each planter is in a restricted project
