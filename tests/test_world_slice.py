"""Project slices: the export's ``slices`` and ``world slice``'s knowledge drafts."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world
from enterprise_sim.worlds.slice import (
    MATERIALIZED_DOCS,
    SliceError,
    canonical_token,
    slice_name,
    slice_world,
)

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
    from enterprise_sim.worlds.slice import core_slice_name

    projects = (_PROJECT, "project:sell-merchandise-retail")
    full = {slice_name(p) for p in projects}
    assert full <= {s["name"] for s in slices}
    assert {s["name"] for s in slices} <= full | {core_slice_name(p) for p in projects}
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
        assert canonical_token(value) and "_" not in value and "," not in value
        for source in item["derived_from"]:
            assert source.startswith(f"{MATERIALIZED_DOCS}/")
            exported = export / source.removeprefix(f"{MATERIALIZED_DOCS}/")
            lines = exported.read_text(encoding="utf-8").splitlines()
            assert any(value in line for line in lines), (source, value)  # one line (TS-DISCOVERED)
            assert item["text"] not in exported.read_text(encoding="utf-8")
    answers = json.loads((out / "answers.json").read_text(encoding="utf-8"))
    for fact in answers["facts"]:
        assert fact["current"]["value"] == fact["history"][-1]["value"]
        assert fact["valid_from"] == fact["history"][-1]["valid_from"]
        assert fact["value_kind"] in ("person", "canonical")
        # the leak report finds the current value on every line that states it.
        for occurrence in fact["occurrences"]:
            text = (export / occurrence["path"].removeprefix(f"{MATERIALIZED_DOCS}/")).read_text(
                encoding="utf-8"
            )
            line = text.splitlines()[occurrence["line"] - 1]
            assert fact["current"]["canonical"].casefold() in line.casefold()
        for stale in fact["stale_values"]:
            assert stale["value"] != fact["current"]["value"]
            assert stale["stance"] in ("stale", "conflicting") and stale["documents"]
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


def test_each_project_slice_carries_a_dated_trail(world_dir: Path, tmp_path: Path) -> None:
    import csv

    result = export_world(world_dir, tmp_path / "out")
    manifest = json.loads((result.directory / "export.json").read_text(encoding="utf-8"))
    trails = {t["name"]: t for t in manifest["tables"] if t["path"].startswith("trails/")}
    assert all(t["kind"] == "table" and t["primary_key"] == [] for t in trails.values())
    assert f"trail.{slice_name(_PROJECT).replace('-', '_')}" in trails
    for table in manifest["tables"]:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", table["name"]), table["name"]
    slices = {s["name"]: s for s in manifest["slices"]}
    for table in trails.values():
        entry = slices[Path(table["path"]).stem]
        assert table["path"] in entry["files"]
        with (result.directory / table["path"]).open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows
        assert list(rows[0]) == ["logged_at", "author", "kind", "document", "summary"]
        for row in rows:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["logged_at"])
            assert "," not in row["summary"] and row["author"]
            assert row["document"] in entry["files"]


def test_a_misleading_value_is_clean_only_if_no_current_reader_meets_it() -> None:
    from enterprise_sim.worlds.slice import _stale_values

    def edge(doc: str, value: str, stance: str) -> dict:  # type: ignore[type-arg]
        return {"src": doc, "props": {"value": value, "stance": stance}}

    paths = {d: f"environment/docs/simx/{d}.md" for d in ("a", "b", "c")}
    edges = [
        edge("a", "v2.0", "current"),  # states the value true at the cut
        edge("a", "v1.2", "stale"),  # ...and, in the same doc, an old one
        edge("b", "v3.1", "stale"),  # only stated by a doc that never states v2.0
        edge("c", "v4.0", "conflicting"),
    ]
    found = {o["value"]: o for o in _stale_values(edges, paths, {"value": "v2.0"})}
    assert set(found) == {"v1.2", "v3.1", "v4.0"}
    assert found["v1.2"]["clean"] is False
    assert found["v3.1"]["clean"] is True and found["v4.0"]["stance"] == "conflicting"


@pytest.mark.parametrize(
    ("token", "ok"),
    [
        ("v2.3", True),
        ("2026-05-14", True),
        ("$250k", True),
        ("v3", False),
        ("fct_orders", False),
        ("1,200", False),
        ("two words", False),
        ("PostgreSQL", True),
    ],
)
def test_canonical_tokens_follow_ts_discovered(token: str, ok: bool) -> None:
    assert canonical_token(token) is ok


def test_each_project_slice_carries_its_fact_registry(world_dir: Path, tmp_path: Path) -> None:
    import csv

    result = export_world(world_dir, tmp_path / "out")
    manifest = json.loads((result.directory / "export.json").read_text(encoding="utf-8"))
    registries = [t for t in manifest["tables"] if t["path"].startswith("registry/")]
    assert registries
    slices = {s["name"]: s for s in manifest["slices"]}
    for table in registries:
        assert table["kind"] == "table" and table["primary_key"] == ["attribute", "version"]
        assert table["path"] in slices[Path(table["path"]).stem]["files"]
        with (result.directory / table["path"]).open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        keys = [(r["attribute"], r["version"]) for r in rows]
        assert len(keys) == len(set(keys))
        for row in rows:
            assert row["canonical"] in row["value"].replace(";", ",")
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["valid_from"])


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("Search Platform", "search"),
        ("sessions", "session"),
        ("Release Notes", "note"),
        ("Build Software Delivery", None),
        ("text_search", "search"),
    ],
)
def test_vocabulary_conflicts(name: str, words: str | None) -> None:
    from enterprise_sim.worlds.slice import vocabulary_conflicts

    found = vocabulary_conflicts([name])
    assert (found[0]["words"] if found else None) == words


def test_slice_answers_report_vocabulary_conflicts(world_dir: Path, tmp_path: Path) -> None:
    slice_world(world_dir, _PROJECT, tmp_path / "v")
    answers = json.loads((tmp_path / "v" / "answers.json").read_text(encoding="utf-8"))
    assert isinstance(answers["vocabulary_conflicts"], list)


def test_core_slices_are_casebook_size(world_dir: Path, tmp_path: Path) -> None:
    from enterprise_sim.worlds.slice import CORE_DOCS, core_slice_name

    export = export_world(world_dir, tmp_path / "out").directory
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    slices = {s["name"]: s for s in manifest["slices"]}
    core = slices[core_slice_name(_PROJECT)]
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", core["name"])
    docs = [f for f in core["files"] if not f.startswith(("registry/", "trails/"))]
    assert 0 < len(docs) <= CORE_DOCS
    assert any(f.startswith("registry/") for f in core["files"])
    assert sum((export / f).stat().st_size for f in core["files"]) < 100_000
    assert set(core["files"]) <= set(slices[slice_name(_PROJECT)]["files"])

    out = tmp_path / "core"
    summary = slice_world(world_dir, _PROJECT, out, core=True)
    answers = json.loads((out / "answers.json").read_text(encoding="utf-8"))
    assert answers["slices"] == [core.get("name")]
    assert summary["knowledge"] > 0
    for fact in answers["facts"]:
        assert len(fact["history"]) >= 2  # every drawn fact has a superseded value
        for paths in fact["documents"].values():
            for path in paths:
                assert path.removeprefix(f"{MATERIALIZED_DOCS}/") in core["files"]


@pytest.mark.parametrize(
    ("value", "typed"),
    [
        ("2026-05-14", {"type": "date", "value": "2026-05-14"}),
        ("$1.2M", {"type": "usd", "value": 1_200_000}),
        ("$250k", {"type": "usd", "value": 250_000}),
        ("daily at 06:00 UTC", {"type": "time_utc", "value": "0600"}),
        ("400 ms", {"type": "count", "value": 400, "unit": "ms"}),
        ("38%", {"type": "percent", "value": 38}),
        ("every two weeks", {"type": "cadence", "value": "biweekly"}),
        ("v2.3", {"type": "version", "value": "v2.3"}),
    ],
)
def test_typed_values(value: str, typed: dict) -> None:  # type: ignore[type-arg]
    from enterprise_sim.worlds.slice import typed_value

    assert typed_value(value) == typed


@pytest.mark.parametrize(
    ("token", "ok"), [("weekly", False), ("track", False), ("v3.2", True), ("CockroachDB", True)]
)
def test_distinctive_tokens(token: str, ok: bool) -> None:
    from enterprise_sim.worlds.slice import distinctive_token

    assert distinctive_token(token) is ok


def test_answers_flag_shared_values_and_kinds(world_dir: Path, tmp_path: Path) -> None:
    slice_world(world_dir, _PROJECT, tmp_path / "f")
    answers = json.loads((tmp_path / "f" / "answers.json").read_text(encoding="utf-8"))
    tokens = [f["current"]["canonical"].casefold() for f in answers["facts"]]
    for fact in answers["facts"]:
        shared = tokens.count(fact["current"]["canonical"].casefold()) > 1
        assert bool(fact["shared_with"]) is shared
        assert all(
            o["kind"] in ("document", "chat", "code", "registry", "trail")
            for o in fact["occurrences"]
        )
        dates = [d["date"] for d in fact["documents_by_version"]]
        assert dates == sorted(dates)
    knowledge = tomllib.loads((tmp_path / "f" / "knowledge.draft.toml").read_text(encoding="utf-8"))
    from enterprise_sim.worlds.slice import distinctive_token

    assert all(distinctive_token(k["values"][0]) for k in knowledge.get("knowledge", []))


def test_observations_grade_differently_under_stale_and_current_targets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "seed = 6\n"
        '[company]\nname = "Obs Co"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-05-29\n"
        '[model]\noffline_backend = "synth"\n'
        "[facts]\nper_project = 11\nchange_rate = 1.0\nmax_versions = 3\n",
        encoding="utf-8",
    )
    root = tmp_path / "w"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    export = export_world(world_dir, tmp_path / "out").directory
    manifest = json.loads((export / "export.json").read_text(encoding="utf-8"))
    tables = [t for t in manifest["tables"] if t["path"].startswith("observations/")]
    assert tables and all(t["primary_key"] == ["date", "attribute"] for t in tables)
    slice_world(world_dir, _PROJECT, tmp_path / "s")
    answers = json.loads((tmp_path / "s" / "answers.json").read_text(encoding="utf-8"))
    graded = [f for f in answers["facts"] if "observations" in f]
    assert graded
    for fact in graded:
        counts = list(fact["observations"]["days_over"].values())
        assert len(counts) >= 2 and len(set(counts)) >= 2  # versions grade differently
