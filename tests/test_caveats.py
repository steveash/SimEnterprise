"""Caveats: a decision's exception or adjustment that changes the value a consumer must use."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
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
        tmp_path_factory.mktemp("caveats") / "a",
        # every ISO date in prose is reformatted, except the values a document must state
        "\n[caveats]\ncount = 6\n\n[concurrency]\ndays = 2\n"
        "\n[access]\nrestricted_projects = 1\n\n[messiness]\ndate_format_mix = 1.0\n",
    )
    out = tmp_path_factory.mktemp("caveats-export")
    return export_world(world, out, with_access=True, with_knowledge=True).directory


def _nodes(export: Path) -> dict[str, Any]:
    lines = (export / "kg" / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
    return {n["id"]: n for n in map(json.loads, lines)}


def _layer(export: Path) -> dict[str, Any]:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE

    return dict(json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8")))


def _unit(typed: dict[str, Any]) -> int:
    """The value after in its shown unit ($k below $1M, $10k above; ms; %)."""
    amount = int(typed["amount"])
    if typed["type"] != "money":
        return amount
    return amount // 10_000 if amount >= 1_000_000 else amount // 1000


def test_each_caveat_changes_a_value_the_chain_never_had(export: Path) -> None:
    nodes = _nodes(export)
    caveats = [n for n in nodes.values() if n["type"] == "Caveat"]
    assert len(caveats) >= 3
    assert len({n["props"]["fact"].rsplit(":", 1)[1] for n in caveats}) >= 2  # attributes
    for n in caveats:
        p = n["props"]
        chain = [
            v["props"]["value"]
            for v in nodes.values()
            if v["type"] == "FactVersion" and v["id"].startswith(f"{p['fact']}@v")
        ]
        assert p["value_before"] in chain and p["value_after"] not in chain
        version = nodes[f"{p['fact']}@v{p['version']}"]["props"]
        assert version["value"] == p["value_before"]
        assert version["valid_from"] <= p["valid_from"]
        assert version["valid_to"] is None or p["valid_from"] < version["valid_to"]
        typed, math = p["typed_after"], p["arithmetic"]
        if typed["type"] == "date":
            base = date.fromisoformat(math["base"])
            assert (date.fromisoformat(typed["value"]) - base).days == math["by"]
            assert math["by"] % 5 and math["by"] % 7  # not a round number of days or weeks
        else:
            assert _unit(typed) % 5  # never round in its unit
        if p["kind"] == "adjustment" and typed["type"] == "money":
            assert math["base"] - math["by"] == math["result"] == typed["amount"]


def test_the_layer_lists_each_caveat_with_its_carriers_and_silent_ones(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import check_layer

    layer = _layer(export)
    assert check_layer(export, layer) == []
    items = {i["id"]: i for i in layer["items"]}
    assert layer["counts"]["caveats"] == len(layer["caveats"]) > 0
    for c in layer["caveats"]:
        assert items[c["item"]]["caveats"].count(c["id"]) == 1
        assert c["value_before"] == items[c["item"]]["value"]
        assert c["carriers"] and all(x["verbatim"] for x in c["carriers"])
        for x in c["carriers"]:
            text = (export / x["path"]).read_text(encoding="utf-8")
            assert c["value_before"] in text and c["value_after"] in text
            assert ("readers" in x) is (not x["everyone"])
        assert "everyone" in c["reach"]
        for s in c["silent_carriers"]:  # later documents that state the base value only
            assert s["at"] >= c["valid_from"][:10]
            assert c["value_after"] not in (export / s["path"]).read_text(encoding="utf-8")
            assert s["path"] in {x["path"] for x in items[c["item"]]["carriers"]}
    assert any(c["silent_carriers"] for c in layer["caveats"])
    for c in layer["caveats"]:  # a second fact: the project's own, reached through a table
        for extra in c["costly_facts"]:
            other = items[extra["item"]]
            assert other["subject"]["id"] == items[c["item"]]["subject"]["id"]
            assert other["fact"] != c["fact"] and extra["tables"]
            for t in extra["tables"]:  # the whole value, never only a canonical fragment
                assert extra["value"] in (export / t).read_text(encoding="utf-8"), (extra, t)
            assert extra["holders"] == [h["person"] for h in other["holders"]]
            assert other["valid_from"] <= c["valid_from"]
    caveat_items = {c["item"] for c in layer["caveats"]}
    assert all("caveats" not in i for i in layer["items"] if i["id"] not in caveat_items)


def test_a_caveat_never_takes_a_concurrent_days_fact(export: Path) -> None:
    nodes = _nodes(export)
    days = {n["props"]["fact"] for n in nodes.values() if n["type"] == "ConcurrentDay"}
    caveats = {n["props"]["fact"] for n in nodes.values() if n["type"] == "Caveat"}
    assert days and caveats and not days & caveats


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["caveats"][0]["silent_carriers"].clear(),
        lambda d: d["caveats"][0].update(value_after="$1k"),
        lambda d: d["caveats"].pop(0),
        lambda d: next(i for i in d["items"] if "caveats" in i).pop("caveats"),
        lambda d: d["items"][0].update(caveats=["caveat:none"]),
        lambda d: d["caveats"][0]["carriers"][0].update(verbatim=False),
    ],
)
def test_the_check_refuses_an_edited_caveat(export: Path, edit: Any) -> None:
    from enterprise_sim.worlds.knowledge import check_layer

    layer = json.loads(json.dumps(_layer(export)))
    edit(layer)
    assert any("caveats" in p for p in check_layer(export, layer))


def test_without_caveats_nothing_changes(tmp_path: Path) -> None:
    from enterprise_sim.assembly import canonical_config
    from enterprise_sim.core.config import load_config
    from enterprise_sim.worlds.knowledge import build_layer

    assert "caveats" not in canonical_config(load_config(_EXAMPLES / "ladder" / "ladder-s.toml"))
    world = _world(tmp_path / "p", "")
    out = export_world(world, tmp_path / "out").directory
    assert not any(n["type"] == "Caveat" for n in _nodes(out).values())
    layer = build_layer(out)
    assert "caveats" not in layer and "caveats" not in layer["counts"]
    assert all("caveats" not in i for i in layer["items"])


def test_the_caveat_document_is_dated_inside_its_version(export: Path) -> None:
    nodes = _nodes(export)
    for c in _layer(export)["caveats"]:
        at = datetime.fromisoformat(c["valid_from"])
        for x in c["carriers"]:
            assert datetime.fromisoformat(nodes[x["artifact"]]["created_at"]) == at
