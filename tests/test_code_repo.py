"""``[code]``: repository artifacts consistent with the fact ledger."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 5\n"
        '[company]\nname = "Repo Co"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-05-29\n"
        '[model]\noffline_backend = "synth"\n'
        "[facts]\nper_project = 11\nchange_rate = 0.9\nmax_versions = 4\n" + body,
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_repositories_carry_current_config_and_a_stale_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys, "[code]\nenabled = true\n")
    nodes = {n["id"]: n for n in _jsonl(world_dir / "kg" / "nodes.jsonl")}
    edges = _jsonl(world_dir / "kg" / "edges.jsonl")
    repo = [
        n for n in nodes.values() if n["type"] == "Artifact" and n["props"].get("medium") == "code"
    ]
    kinds = {n["props"]["kind"] for n in repo}
    assert {"service_config", "readme", "commit_log"} <= kinds
    versions: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node["type"] == "FactVersion":
            versions.setdefault(node["props"]["fact"], []).append(node["props"])
    for node in repo:
        path = world_dir / node["props"]["path"]
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        if node["props"]["kind"] == "service_config":
            tomllib.loads(text)
        for edge in (e for e in edges if e["type"] == "asserts" and e["src"] == node["id"]):
            assert edge["props"]["value"] in text
            latest = max(versions[edge["dst"]], key=lambda v: v["version"])
            if edge["props"]["stance"] == "current":
                assert edge["props"]["value"] == latest["value"]
            if edge["props"]["stance"] == "stale":
                assert edge["props"]["stated_version"] < latest["version"]
    manifest = json.loads(
        (export_world(world_dir, tmp_path / "out").directory / "export.json").read_text()
    )
    rendered = {f.get("rendered_from") for f in manifest["files"]}
    assert any(str(r).endswith("service.toml") for r in rendered)


def test_no_repository_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world_dir = _build(tmp_path, capsys, "")
    assert not list((world_dir / "artifacts").rglob("repo"))
