"""``[formats]``: kit kinds rendered as decks, e-mail threads and meeting invites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], formats: str) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 8\n"
        '[company]\nname = "Format Co"\nvertical = "software"\nsize = "medium"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-03-06\n"
        '[model]\noffline_backend = "synth"\n[facts]\nper_project = 5\n'
        '[[departments]]\narchetype = "sales"\n[[departments]]\narchetype = "support"\n'
        '[[departments]]\narchetype = "data_analytics"\n[[departments]]\narchetype = "security"\n'
        + formats,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_formats_render_native_files_with_facts_and_authors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys, "[formats]\ndecks = true\nemail = true\ninvites = true\n")
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    authored = {e["dst"] for e in edges if e["type"] == "authored"}
    by_suffix: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "Artifact":
            by_suffix.setdefault(Path(node["props"]["path"]).suffix, []).append(node)
    assert {".eml", ".ics", ".pptx"} <= set(by_suffix)
    kinds = {
        ".eml": {
            "escalation_note",
            "incident_comms",
            "pricing_exception",
            "purchase_request",
            "offer_approval",
        },
        ".ics": {"forecast_call", "csat_review", "access_review"},
        ".pptx": {"business_review", "board_pack", "launch_plan"},
    }
    for suffix, allowed in kinds.items():
        for node in by_suffix[suffix]:
            assert node["props"]["kind"] in allowed
            assert (world_dir / node["props"]["path"]).is_file()
            assert node["id"] in authored
    # facts reach the new formats too
    asserted = {e["src"] for e in edges if e["type"] == "asserts"}
    assert any(n["id"] in asserted for n in by_suffix[".eml"])
    # the export renders them as text, with no e-mail addresses
    export = export_world(world_dir, tmp_path / "out").directory
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    rendered = {f["rendered_from"] for f in manifest["files"] if "rendered_from" in f}
    for suffix in (".eml", ".ics", ".pptx"):
        for node in by_suffix[suffix]:
            assert node["props"]["path"] in rendered


def test_formats_off_keeps_the_old_bindings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys, "")
    suffixes = {p.suffix for p in (world_dir / "artifacts").rglob("*") if p.is_file()}
    assert not suffixes & {".eml", ".ics", ".pptx"}
