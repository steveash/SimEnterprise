"""``[org] homonyms``: planted same-name pairs, labelled, and resolved by document context."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


def _build(root: Path, count: int) -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / "analytics-structured-clean.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text = re.sub(r"(?ms)^\[org\]\n(?:(?!^\[).)*", "", text)
    (root / "cfg.toml").write_text(text + f"\n[org]\nhomonyms = {count}\n", encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world_dir,) = list((root / "w").iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("homonyms") / "a", 3)


def test_pairs_share_a_name_across_departments_and_are_labelled(world: Path) -> None:
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    edges = _jsonl(world / "kg" / "edges.jsonl")
    pairs = {(e["src"], e["dst"]) for e in edges if e["type"] == "homonym_of"}
    assert len(pairs) == 6  # three pairs, both directions
    leads = {e["src"] for e in edges if e["type"] == "leads"}
    for a, b in pairs:
        assert (b, a) in pairs
        assert nodes[a]["props"]["name"] == nodes[b]["props"]["name"]
        assert nodes[a]["props"]["department"] != nodes[b]["props"]["department"]
        assert a not in leads and b not in leads


def test_every_mention_of_a_shared_name_resolves_by_context(world: Path) -> None:
    nodes = {n["id"]: n for n in _jsonl(world / "kg" / "nodes.jsonl")}
    edges = _jsonl(world / "kg" / "edges.jsonl")
    events = {e["id"]: e for e in _jsonl(world / "kg" / "events.jsonl")}
    twins = {e["src"] for e in edges if e["type"] == "homonym_of"}
    shared = {nodes[t]["props"]["name"] for t in twins}
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    by_path = {str(n["props"].get("path")): n for n in nodes.values() if n["type"] == "Artifact"}
    checked = 0
    for mention in _jsonl(world / "kg" / "mentions.jsonl"):
        if mention["surface_form"] not in shared:
            continue
        artifact = by_path[mention["artifact_path"]]
        project = nodes[project_of[artifact["id"]]]
        person = nodes[mention["entity_id"]]
        event = events.get(str(artifact["props"].get("event")), {})
        actors = {p for ids in event.get("actors", {}).values() for p in ids}
        assert person["id"] in twins
        assert (
            person["props"]["department"] == project["props"]["department"]
            or person["id"] in actors
        ), (mention, project["props"]["department"])
        checked += 1
    assert checked > 0, "the planted names must appear in documents"


def test_no_homonyms_by_default(tmp_path: Path) -> None:
    world = _build(tmp_path / "off", 0)
    edges = _jsonl(world / "kg" / "edges.jsonl")
    assert not [e for e in edges if e["type"] == "homonym_of"]


def test_homonyms_change_no_world_id_when_off() -> None:
    from enterprise_sim.assembly import compute_config_digest
    from enterprise_sim.core.config import WorldConfig, load_config

    config = load_config(_EXAMPLES / "analytics-structured-clean.toml")
    explicit = WorldConfig.model_validate(
        {**config.model_dump(mode="json"), "org": {**config.org.model_dump(), "homonyms": 0}}
    )
    assert compute_config_digest(explicit) == compute_config_digest(config)
