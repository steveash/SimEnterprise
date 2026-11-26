"""Per-document reader lists (decision record 0130): computed once, gold, checked closed."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.access import ACCESS_FILE, access_lists, check_access
from enterprise_sim.exporters.text_export import ExportError, export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"
_RATE = 0.3


def _world(root: Path, extra: str) -> Path:
    root.mkdir(parents=True)
    text = (_EXAMPLES / "walled-small-legal.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[data\]\n(?:(?!^\[).)*", "", text)
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    (root / "cfg.toml").write_text(text + extra, encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return world


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _world(
        tmp_path_factory.mktemp("access") / "a",
        f"\n[access]\nteam_restricted_rate = {_RATE}\n",
    )


@pytest.fixture(scope="module")
def export(world: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("access-export")
    return export_world(world, out, with_access=True, with_knowledge=True).directory


def _graph(export: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    nodes = {
        n["id"]: n
        for n in map(json.loads, (export / "kg" / "nodes.jsonl").read_text().splitlines())
    }
    edges = [json.loads(line) for line in (export / "kg" / "edges.jsonl").read_text().splitlines()]
    config = json.loads((export / "config.json").read_text(encoding="utf-8"))
    return nodes, edges, config


def test_the_lists_are_gold_and_hold(export: Path) -> None:
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    (entry,) = [f for f in manifest["files"] if f["path"] == ACCESS_FILE]
    assert entry["role"] == "gold"
    assert all(ACCESS_FILE not in s["files"] for s in manifest["slices"])  # controller-only
    assert check_access(export) == []
    listed = json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))
    reasons = {r for row in listed["documents"] for r in row["reasons"]}
    assert {"walled", "team"} <= reasons
    for row in listed["documents"]:
        assert row["readers"] and row["readers"] == sorted(set(row["readers"]))


def test_every_rule_keeps_its_readers(export: Path) -> None:
    nodes, edges, config = _graph(export)
    lists = access_lists(nodes, edges, config)
    authors: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "authored":
            authors.setdefault(e["dst"], set()).add(e["src"])
    project_of = {e["src"]: e["dst"] for e in edges if e["type"] == "expresses"}
    staff: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] == "member_of" and nodes[e["dst"]]["type"] == "Project":
            staff.setdefault(e["dst"], set()).add(e["src"])
    artifacts = [i for i, n in nodes.items() if n["type"] == "Artifact"]
    walled = {i for i, n in nodes.items() if n["type"] == "Project" and n["props"].get("walled")}
    for aid in artifacts:
        project = project_of.get(aid)
        if project in walled:  # every walled document is restricted to its staff
            assert set(lists[aid]["readers"]) <= staff[project], aid
        if aid in lists:
            assert authors[aid] <= set(lists[aid]["readers"])
    unwalled = [a for a in artifacts if project_of.get(a) not in walled]
    teamed = [a for a in unwalled if "team" in lists.get(a, {}).get("reasons", ())]
    assert 0.15 < len(teamed) / len(unwalled) < 0.45  # about the configured share


def test_the_knowledge_layer_carries_the_same_readers(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE, check_layer

    layer = json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    assert check_layer(export, layer) == []
    listed = {
        r["path"]: r["readers"]
        for r in json.loads((export / ACCESS_FILE).read_text(encoding="utf-8"))["documents"]
    }
    carriers = [c for i in layer["items"] for c in i["carriers"]]
    assert any("readers" in c for c in carriers)
    for c in carriers:
        assert c.get("readers") == listed.get(c["path"])


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["documents"][0].update(readers=[]),
        lambda d: d["documents"][0]["readers"].append("person:nobody"),
        lambda d: d["documents"][0].update(readers=d["documents"][0]["readers"][1:]),
    ],
)
def test_the_check_fails_closed(export: Path, tmp_path: Path, edit: Any) -> None:
    edited = tmp_path / "e"
    shutil.copytree(export, edited)
    body = json.loads((edited / ACCESS_FILE).read_text(encoding="utf-8"))
    edit(body)
    (edited / ACCESS_FILE).write_text(json.dumps(body), encoding="utf-8")
    assert check_access(edited)
    assert main(["world", "access-check", str(edited)]) == 1


def test_an_export_refuses_a_list_that_does_not_hold(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enterprise_sim.exporters import access

    real = access.access_document

    def emptied(*args: Any) -> dict[str, Any]:
        body = real(*args)
        body["documents"][0]["readers"] = []
        return body

    monkeypatch.setattr(access, "access_document", emptied)
    with pytest.raises(ExportError, match="reader lists do not hold"):
        export_world(world, tmp_path / "out", with_access=True)


def test_without_the_flag_and_at_rate_zero_nothing_changes(tmp_path: Path) -> None:
    from enterprise_sim.assembly import canonical_config, compute_config_digest
    from enterprise_sim.core.config import WorldConfig, load_config

    plain = load_config(_EXAMPLES / "walled-small-legal.toml")
    rated = WorldConfig.model_validate(
        {**plain.model_dump(mode="json"), "access": {"team_restricted_rate": 0.4}}
    )
    assert compute_config_digest(rated) == compute_config_digest(plain)  # not in the world id
    assert "access" not in canonical_config(plain)  # at its default: not even a key
    world = _world(tmp_path / "p", "")
    out = export_world(world, tmp_path / "out").directory
    assert not (out / ACCESS_FILE).exists()
