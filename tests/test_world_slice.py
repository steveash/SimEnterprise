"""Project slices: the export's ``slices`` and ``world slice``'s knowledge drafts."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world
from enterprise_sim.worlds.slice import MATERIALIZED_DOCS, SliceError, slice_name, slice_world

_PROJECT = "project:build-software-engineering"


@pytest.fixture
def world_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 4\n"
        '[company]\nname = "Slice Co"\nvertical = "software"\nsize = "medium"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-03-27\n"
        '[model]\noffline_backend = "synth"\n'
        "[facts]\nper_project = 8\nchange_rate = 0.6\nstale_rate = 0.3\nconflict_rate = 0.1\n"
        "cross_team = 1.0\n",
        encoding="utf-8",
    )
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (found,) = list(root.iterdir())
    return found


def test_the_export_declares_one_slice_per_project(world_dir: Path, tmp_path: Path) -> None:
    result = export_world(world_dir, tmp_path / "out")
    manifest = json.loads((result.directory / "export.json").read_text(encoding="utf-8"))
    files = {f["path"]: f for f in manifest["files"]}
    slices = manifest["slices"]
    assert {s["name"] for s in slices} == {
        slice_name(_PROJECT),
        slice_name("project:sell-merchandise-retail"),
    }
    for entry in slices:
        assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", entry["name"])
        assert entry["files"]
        for path in entry["files"]:
            assert files[path]["role"] == "data"


@pytest.mark.parametrize(
    "project",
    [
        "project:build-software-engineering",
        "project:run-data-engineering-data-engineering",
        "project:9-lives",
        "project:" + "x" * 80,
    ],
)
def test_slice_names_follow_the_catalog_rule(project: str) -> None:
    name = slice_name(project)
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name)
    assert slice_name(project) == name


def test_knowledge_drafts_point_at_materialized_paths_that_state_the_value(
    world_dir: Path, tmp_path: Path
) -> None:
    export = export_world(world_dir, tmp_path / "export").directory
    out = tmp_path / "draft"
    summary = slice_world(world_dir, _PROJECT, out, with_dependencies=True)
    assert summary["knowledge"] > 0
    assert not (out / "environment").exists()
    knowledge = tomllib.loads((out / "knowledge.draft.toml").read_text(encoding="utf-8"))
    for item in knowledge["knowledge"]:
        assert item["owner"] == "environment" and item["derived_from"]
        (value,) = item["values"]
        assert len(value) >= 3 and not any(ch.isspace() for ch in value)
        for source in item["derived_from"]:
            assert source.startswith(f"{MATERIALIZED_DOCS}/")
            exported = export / source.removeprefix(f"{MATERIALIZED_DOCS}/")
            lines = exported.read_text(encoding="utf-8").splitlines()
            assert any(value in line for line in lines), (source, value)  # one line (TS-DISCOVERED)
            assert item["text"] not in exported.read_text(encoding="utf-8")
    answers = json.loads((out / "answers.json").read_text(encoding="utf-8"))
    for fact in answers["facts"]:
        assert fact["current"]["value"] == fact["history"][-1]["value"]
        for paths in fact["documents"].values():
            for path in paths:
                assert (export / path.removeprefix(f"{MATERIALIZED_DOCS}/")).is_file()


def test_an_earlier_cut_only_sees_earlier_versions(world_dir: Path, tmp_path: Path) -> None:
    slice_world(world_dir, _PROJECT, tmp_path / "s", at="2026-02-01")
    answers = json.loads((tmp_path / "s" / "answers.json").read_text(encoding="utf-8"))
    assert answers["facts"]
    for fact in answers["facts"]:
        assert all(v["valid_from"][:10] <= "2026-02-01" for v in fact["history"])


def test_slice_refuses_an_unknown_project_and_a_used_target(
    world_dir: Path, tmp_path: Path
) -> None:
    with pytest.raises(SliceError, match="not a project"):
        slice_world(world_dir, "project:nope", tmp_path / "a")
    slice_world(world_dir, _PROJECT, tmp_path / "b")
    with pytest.raises(SliceError, match="not empty"):
        slice_world(world_dir, _PROJECT, tmp_path / "b")


def test_world_slice_cli(
    world_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["world", "slice", str(world_dir), "--project", _PROJECT]
    assert main([*args, "--out", str(tmp_path / "c")]) == 0
    assert json.loads(capsys.readouterr().out)["knowledge"] > 0
    assert (
        main(["world", "slice", str(world_dir), "--project", "x", "--out", str(tmp_path / "d")])
        == 2
    )
