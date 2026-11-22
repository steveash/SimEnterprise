"""``[chat]``: team-channel threads beside documents, one fact per message."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 9\n"
        '[company]\nname = "Chat Co"\nvertical = "software"\nsize = "medium"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-27\n"
        '[model]\noffline_backend = "synth"\n[facts]\nper_project = 6\n' + body,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_chat_threads_state_one_fact_per_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys, "[chat]\nrate = 0.5\n")
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    threads = [
        n for n in nodes.values() if n["type"] == "Artifact" and n["props"]["kind"] == "chat_thread"
    ]
    documents = [
        n for n in nodes.values() if n["type"] == "Artifact" and n["props"]["kind"] != "chat_thread"
    ]
    assert threads and 0.2 < len(threads) / len(documents) < 0.8
    authored = {e["dst"] for e in edges if e["type"] == "authored"}
    asserts: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge["type"] == "asserts":
            asserts.setdefault(edge["src"], []).append(edge)
    for thread in threads:
        assert thread["id"].endswith(":chat") and thread["id"] in authored
        messages = _jsonl(world_dir / thread["props"]["path"])
        assert messages[0]["thread"] == messages[0]["ts"]
        assert all(m["channel"].startswith("#") and "@" not in m["user"] for m in messages)
        assert [m["ts"] for m in messages] == sorted(m["ts"] for m in messages)
        for edge in asserts.get(thread["id"], []):  # each fact value sits in one message
            assert sum(edge["props"]["value"] in m["text"] for m in messages[1:]) >= 1
    export = export_world(world_dir, tmp_path / "out").directory
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    paths = {f.get("source_path", f["path"]) for f in manifest["files"]}
    assert {t["props"]["path"] for t in threads} <= paths


def test_no_chat_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world_dir = _build(tmp_path, capsys, "")
    assert not list((world_dir / "artifacts").rglob("*.chat.jsonl"))
