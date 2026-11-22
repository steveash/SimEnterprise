"""``[code] sessions_per_project``: coding-agent session transcripts, consistent with the ledger."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


def _config(sessions: int) -> str:
    text = (_EXAMPLES / "coding-team-clean.toml").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^worlds_root = .*$", "", text)
    for section in ("code", "facts"):
        text = re.sub(rf"(?ms)^\[{section}\]\n(?:(?!^\[).)*", "", text)
    text += "\n[facts]\nper_project = 8\nchange_rate = 0.9\nmax_versions = 4\n"
    text += f"\n[code]\nenabled = true\nsessions_per_project = {sessions}\n"
    return text


def _build(root: Path, sessions: int) -> Path:
    root.mkdir(parents=True)
    cfg = root / "cfg.toml"
    cfg.write_text(_config(sessions), encoding="utf-8")
    assert main(["world", "create", str(cfg), "--worlds-root", str(root / "w")]) == 0
    (world_dir,) = list((root / "w").iterdir())
    return world_dir


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("sessions") / "a", 10)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sessions(world: Path) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    nodes = _jsonl(world / "kg" / "nodes.jsonl")
    found = []
    for node in nodes:
        if node["type"] == "Artifact" and node["props"].get("kind") == "agent_session":
            found.append((node, _jsonl(world / node["props"]["path"])))
    return found


def test_sessions_are_well_formed_turns(world: Path) -> None:
    sessions = _sessions(world)
    assert sessions
    for node, turns in sessions:
        assert [t["turn"] for t in turns] == list(range(1, len(turns) + 1))
        assert turns[0]["role"] == "user" and turns[0]["type"] == "message"
        for turn in turns:
            assert turn["role"] in ("user", "assistant", "tool")
            assert turn["type"] in ("message", "tool_use", "tool_result")
            if turn["type"] == "tool_use":
                assert turn["tool"] in ("read_file", "edit_file", "run_command") and turn["input"]
        assert node["props"]["authors"], node["id"]


def test_every_labelled_value_is_in_the_session_and_true_when_read(world: Path) -> None:
    edges = _jsonl(world / "kg" / "edges.jsonl")
    sessions = {node["id"]: (node, turns) for node, turns in _sessions(world)}
    checked = 0
    for edge in edges:
        if edge["type"] != "asserts" or edge["src"] not in sessions:
            continue
        _, turns = sessions[edge["src"]]
        text = "\n".join(json.dumps(t, ensure_ascii=False) for t in turns)
        assert edge["props"]["value"] in text, edge
        if edge["props"]["stance"] == "current":
            assert edge["props"]["stated_version"] == edge["props"]["true_version"], edge
        checked += 1
    assert checked > 0


def test_a_change_session_reads_the_old_value_and_writes_the_new(world: Path) -> None:
    edges = _jsonl(world / "kg" / "edges.jsonl")
    sessions = {node["id"]: turns for node, turns in _sessions(world)}
    updates = [
        e
        for e in edges
        if e["type"] == "asserts" and e["src"] in sessions and e["props"]["stance"] == "update"
    ]
    assert updates
    for edge in updates:
        turns = sessions[edge["src"]]
        (edit,) = [t for t in turns if t.get("tool") == "edit_file" and t["type"] == "tool_use"]
        assert json.dumps(edge["props"]["value"]) in edit["input"]["new"]
        read = next(t for t in turns if t["type"] == "tool_result" and t["tool"] == "read_file")
        assert edit["input"]["old"] in read["text"]  # the config showed the old value
        assert edit["input"]["new"] not in read["text"]


def test_a_stale_comment_is_read_and_corrected(world: Path) -> None:
    corrected = [(node, turns) for node, turns in _sessions(world) if node["props"]["corrected"]]
    assert corrected  # the datastore changed before some session in this world
    for _, turns in corrected:
        notes = [t["text"] for t in turns if t["role"] == "assistant" and t["type"] == "message"]
        assert any("stale" in note for note in notes)
        reads = [
            t["text"] for t in turns if t.get("tool") == "read_file" and t["type"] == "tool_result"
        ]
        assert any("Talks to" in r for r in reads)


def test_one_change_per_day_and_sessions_on_commit_days(world: Path) -> None:
    sessions = _sessions(world)
    tasks = [turns[0]["text"] for _, turns in sessions if turns[0]["text"].startswith("Switch")]
    assert len(tasks) == len(set(tasks))
    events = {e["id"]: e for e in _jsonl(world / "kg" / "events.jsonl")}
    for node, _ in sessions:
        event = events[node["props"]["event"]]
        assert event["type"] == "CommitPushed"
        assert (
            datetime.fromisoformat(event["timestamp"]).date().isoformat() == node["props"]["date"]
        )


def test_sessions_export_as_project_data(world: Path, tmp_path: Path) -> None:
    result = export_world(world, tmp_path / "out")
    manifest = json.loads((result.directory / "export.json").read_text(encoding="utf-8"))
    files = {f["path"]: f for f in manifest["files"]}
    exported = [p for p in files if p.endswith(".session.jsonl")]
    assert exported and all(files[p]["role"] == "data" for p in exported)
    in_slices = {p for s in manifest["slices"] for p in s["files"]}
    assert set(exported) <= in_slices


def test_sessions_are_deterministic(world: Path, tmp_path: Path) -> None:
    again = _build(tmp_path / "again", 10)
    first = {p.name: p.read_bytes() for p in world.rglob("*.session.jsonl")}
    second = {p.name: p.read_bytes() for p in again.rglob("*.session.jsonl")}
    assert first and first == second


def test_no_sessions_by_default(tmp_path: Path) -> None:
    world = _build(tmp_path / "off", 0)
    assert not list(world.rglob("*.session.jsonl"))
