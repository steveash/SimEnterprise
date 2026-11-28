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
        # every ISO date in prose is reformatted, except the values a document must state
        "\n[concurrency]\ndays = 2\n\n[access]\nrestricted_projects = 1\n"
        "\n[messiness]\ndate_format_mix = 1.0\ntypo_rate = 0.05\n",
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
                extra["value"] in (export / t).read_text(encoding="utf-8") for t in extra["tables"]
            )
    edited = json.loads(json.dumps(layer))
    edited["workstreams"][0]["discovered_at"] = "2026-01-01T00:00:00"
    assert any("workstreams" in p for p in check_layer(export, edited))


def test_every_workstream_document_states_its_value_as_written(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE, check_layer

    layer = json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    items = {i["id"]: i for i in layer["items"]}
    nodes, _ = _graph(export)
    reformatted = [
        q
        for n in nodes.values()
        for q in n["props"].get("quirks", ())
        if q["kind"] == "date_format"
    ]
    assert reformatted  # the noise ran: dates elsewhere are not as written
    for s in layer["workstreams"]:
        for doc in (s["discovery"], s["needed_by"]["before"], s["needed_by"]["after"]):
            value = items[doc["stated_item"]]["value"]
            assert doc["verbatim"] is True, doc["path"]
            assert value in (export / doc["path"]).read_text(encoding="utf-8")
    edited = json.loads(json.dumps(layer))
    edited["workstreams"][0]["discovery"]["verbatim"] = False
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


def _typed_holds(export: Path, path: str, token: str, until: str) -> bool:
    import csv

    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    (entry,) = [t for t in manifest["tables"] if t["path"] == path]
    types = [c["type"] for c in entry["columns"]]
    rows = list(csv.reader((export / path).read_text(encoding="utf-8").splitlines()))[1:]
    dated = [i for i, k in enumerate(types) if k in ("date", "timestamp")]
    rows = [  # a row written after the window is not evidence at the window
        r for r in rows if not any(r[i] and r[i][: len(until)] > until[: len(r[i])] for i in dated)
    ]
    import re

    whole = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
    return any(
        (types[i] == "text" and whole.search(cell) is not None)
        or (types[i] not in ("text", "date", "timestamp") and cell == token)
        for row in rows
        for i, cell in enumerate(row)
    )


def test_a_table_counts_only_typed_evidence(export: Path) -> None:
    from enterprise_sim.worlds.knowledge import KNOWLEDGE_FILE

    layer = json.loads((export / KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    for s in layer["workstreams"]:
        for path in s["carried_by_window_end"]:
            assert _typed_holds(export, path, s["answer"]["value"], s["window"]["end"]), path
        assert set(s["carried_by_window_end"]) <= set(s["carried_in_tables"])  # any row
        for extra in s["costly_facts"]:
            for path in extra["tables"]:  # never a fragment of a timestamp or a number
                assert _typed_holds(export, path, extra["value"], s["window"]["end"]), (
                    extra["item"],
                    path,
                )


def test_a_row_date_is_never_evidence_of_a_date_value(export: Path) -> None:
    """A daily table has a row for every day: its date column does not state a target date."""
    import csv

    from enterprise_sim.worlds.knowledge import _Export, _table_holds

    ex = _Export(export)
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    probed = 0
    for entry in manifest["tables"]:
        types = [c["type"] for c in entry["columns"]]
        dated = {i for i, k in enumerate(types) if k in ("date", "timestamp")}
        rows = list(csv.reader((export / entry["path"]).read_text(encoding="utf-8").splitlines()))
        other = " ".join(c for r in rows[1:] for j, c in enumerate(r) if j not in dated)
        for i in sorted(dated):
            value = rows[1][i] if len(rows) > 1 else ""
            if value and value not in other:  # held only as the row's own date
                probed += 1
                assert not _table_holds(ex, entry["path"], value), (entry["path"], value)
    assert probed  # the export has dated rows to probe
