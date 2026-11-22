"""``world replay``: an export's config + recorded responses rebuild the world exactly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world
from enterprise_sim.worlds.replay import ReplayError, replay_export


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 4\n"
        '[company]\nname = "Replay Co"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-02-06\n"
        '[model]\noffline_backend = "synth"\n[facts]\nper_project = 5\n[chat]\nrate = 0.3\n',
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


def _artifacts(world_dir: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(world_dir)): p.read_bytes()
        for p in (world_dir / "artifacts").rglob("*")
        if p.is_file()
    }


def test_replay_rebuilds_the_same_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys)
    export = export_world(world_dir, tmp_path / "out", with_replay=True).directory
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    files = {f["path"]: f for f in manifest["files"]}
    assert files["replay/llm-cache.jsonl"]["role"] == "gold"
    rebuilt = replay_export(export, tmp_path / "again")
    assert rebuilt.name == world_dir.name
    assert _artifacts(rebuilt) == _artifacts(world_dir)


def test_replay_of_a_live_world_without_responses_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world_dir = _build(tmp_path, capsys)
    export = export_world(world_dir, tmp_path / "out").directory
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    manifest["render_backend"] = "claude_cli"
    (export / "export.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReplayError, match="needs replay"):
        replay_export(export, tmp_path / "again")


def test_live_replay_refuses_to_call_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from enterprise_sim.worlds.replay import _NoCalls

    with pytest.raises(ReplayError, match="missing from the replay"):
        _NoCalls("claude_cli").generate_content()
