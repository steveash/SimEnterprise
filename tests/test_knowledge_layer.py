"""``world knowledge-layer``: items, holders, carriers and misleading documents of an export."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world
from enterprise_sim.worlds.knowledge import FORMAT, build_layer, check_layer

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("knowledge")
    text = (_EXAMPLES / "ladder" / "ladder-s.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    text += "\n[org]\nturnover_rate = 0.2\n"
    (root / "cfg.toml").write_text(text, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return export_world(world, root / "out").directory


def _world_of(export: Path) -> Path:
    (world,) = list((export.parents[1] / "w").iterdir())
    return world


@pytest.fixture(scope="module")
def layer(export: Path) -> dict[str, Any]:
    return build_layer(export)


def _gold(export: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = (export / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
    nodes = {n["id"]: n for n in map(json.loads, lines)}
    edges = [json.loads(line) for line in (export / "kg" / "edges.jsonl").read_text().splitlines()]
    return nodes, edges


def test_the_layer_holds_against_its_export(export: Path, layer: dict[str, Any]) -> None:
    assert layer["format"] == FORMAT
    assert check_layer(export, layer) == []
    counts = layer["counts"]
    assert counts["items"] > 0 and counts["supersessions"] > 0 and counts["carriers"] > 0
    assert counts["misleading"] > 0 and counts["holders"] > 0


def test_every_gold_statement_is_a_carrier_or_a_misleading_entry(
    export: Path, layer: dict[str, Any]
) -> None:
    _, edges = _gold(export)
    stances = Counter(e["props"]["stance"] for e in edges if e["type"] == "asserts")
    carriers = Counter(c["stance"] for i in layer["items"] for c in i["carriers"])
    assert carriers == {s: stances[s] for s in ("current", "update", "hedged") if stances[s]}
    misleading = Counter(m["kind"] for m in layer["misleading"])
    assert misleading["stale"] == stances["stale"]
    assert misleading["conflicting"] == stances["conflicting"]
    assert misleading["near_duplicate"] == sum(1 for e in edges if e["type"] == "duplicate_of")
    retractions = sum(len(i["retracted_in"]) for i in layer["items"])
    assert retractions == stances["retraction"]


def test_supersessions_follow_the_version_chain(export: Path, layer: dict[str, Any]) -> None:
    nodes, edges = _gold(export)
    gold = {(e["dst"], e["src"]) for e in edges if e["type"] == "supersedes"}
    assert {(s["old"], s["new"]) for s in layer["supersessions"]} == gold
    items = {i["id"]: i for i in layer["items"]}
    for entry in layer["supersessions"]:
        new = items[entry["new"]]
        assert new["supersedes"] == entry["old"] and entry["effective"] == new["valid_from"]
        assert items[entry["old"]]["superseded_by"] == entry["new"]
        assert nodes[entry["new"]]["props"]["valid_from"] == entry["effective"]


def test_holders_are_people_and_announcers_wrote_the_update(
    export: Path, layer: dict[str, Any]
) -> None:
    nodes, edges = _gold(export)
    authored: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "authored":
            authored.setdefault(e["src"], set()).add(e["dst"])
    paths = {n["props"].get("path") for n in nodes.values() if n["type"] == "Artifact"}
    assert paths
    roster = {p["id"] for p in layer["people"]}
    kinds = {"decision", "policy", "target", "status"}
    for item in layer["items"]:
        assert item["kind"] in kinds
        assert {h["person"] for h in item["holders"]} <= roster
        roles = {h["person"]: h["as"] for h in item["holders"]}
        updates = {c["path"] for c in item["carriers"] if c["stance"] == "update"}
        if updates:
            assert "announcer" in roles.values(), item["id"]


def test_the_same_export_gives_the_same_layer(export: Path, layer: dict[str, Any]) -> None:
    assert build_layer(export) == layer


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (
            lambda L: L["items"][0]["carriers"].append(
                {"path": "artifacts/none.md", "stance": "current", "at": "", "verbatim": False}
            ),
            "not in the export",
        ),
        (
            lambda L: L["items"][0]["holders"].append({"person": "person:nobody", "as": "author"}),
            "not in the roster",
        ),
        (lambda L: L.update(export_digest="0" * 64), "another export"),
        (lambda L: L["items"].append(L["items"][0]), "repeats"),
    ],
)
def test_the_check_refuses_a_layer_that_does_not_match(
    export: Path, layer: dict[str, Any], edit: Any, problem: str
) -> None:
    edited = copy.deepcopy(layer)
    edit(edited)
    assert any(problem in p for p in check_layer(export, edited))


def test_the_check_refuses_a_wrong_verbatim_flag_and_a_broken_chain(
    export: Path, layer: dict[str, Any]
) -> None:
    edited = copy.deepcopy(layer)
    carrier = next(c for i in edited["items"] for c in i["carriers"])
    carrier["verbatim"] = not carrier["verbatim"]
    assert any("verbatim" in p for p in check_layer(export, edited))
    edited = copy.deepcopy(layer)
    item = next(i for i in edited["items"] if i["superseded_by"])
    item["superseded_by"] = item["id"] + "x"
    assert any("not an item" in p for p in check_layer(export, edited))


def test_the_cli_writes_a_checked_layer(export: Path, tmp_path: Path) -> None:
    out = tmp_path / "knowledge.json"
    assert main(["world", "knowledge-layer", str(export), "--out", str(out)]) == 0
    assert main(["world", "knowledge-layer", str(export), "--check", str(out)]) == 0
    bad = json.loads(out.read_text(encoding="utf-8"))
    bad["items"][0]["holders"].append({"person": "person:nobody", "as": "author"})
    out.write_text(json.dumps(bad), encoding="utf-8")
    assert main(["world", "knowledge-layer", str(export), "--check", str(out)]) == 1


def test_every_catalog_attribute_has_a_knowledge_kind() -> None:
    from enterprise_sim.facts import catalog

    attributes = [
        a
        for value in vars(catalog).values()
        if isinstance(value, tuple)
        for a in value
        if isinstance(a, catalog.Attribute)
    ]
    assert attributes
    assert {a.kind for a in attributes} == {"decision", "policy", "target", "status"}
    assert all(catalog.kind_of(a.name) == a.kind for a in attributes)


def test_an_export_can_carry_its_own_layer_as_gold(
    export: Path, layer: dict[str, Any], tmp_path: Path
) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE

    assert not (export / KNOWLEDGE_FILE).exists()  # off by default
    inside = export_world(_world_of(export), tmp_path / "out", with_knowledge=True).directory
    manifest = json.loads((inside / "export.json").read_text(encoding="utf-8"))
    (entry,) = [f for f in manifest["files"] if f["path"] == KNOWLEDGE_FILE]
    assert entry["role"] == "gold"
    carried = json.loads((inside / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    assert carried["export_digest"] is None and check_layer(inside, carried) == []
    assert {k: v for k, v in carried.items() if k != "export_digest"} == {
        k: v for k, v in layer.items() if k != "export_digest"
    }
    carried["items"][0]["value"] = "changed"
    assert any(KNOWLEDGE_FILE in p for p in check_layer(inside, carried))
