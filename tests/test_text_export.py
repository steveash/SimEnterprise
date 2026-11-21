"""``world export``: the text-only ``simenterprise-export/1`` directory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import (
    EXPORT_FORMAT,
    ExportError,
    export_world,
    prose_backend_of,
)


def _build(tmp_path: Path, capsys: pytest.CaptureFixture[str], *, data: bool) -> Path:
    lines = [
        "seed = 7",
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"',
        "[simulation]\nperiod_start = 2026-01-05\nperiod_end = 2026-01-16",
    ]
    if data:
        lines.append(
            '[data]\nscenarios = ["support_ops"]\nscale_factor = 0.05\niterations = 1\n'
            "rows_per_partition = 50000"
        )
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    root = tmp_path / "worlds"
    assert main(["world", "create", str(cfg), "--worlds-root", str(root)]) == 0
    capsys.readouterr()
    (world_dir,) = list(root.iterdir())
    return world_dir


@pytest.fixture
def world_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    return _build(tmp_path, capsys, data=True)


def _manifest(export_dir: Path) -> dict:  # type: ignore[type-arg]
    data: dict = json.loads((export_dir / "export.json").read_text(encoding="utf-8"))  # type: ignore[type-arg]
    return data


def test_export_lists_every_file_with_its_digest_and_nothing_else(
    world_dir: Path, tmp_path: Path
) -> None:
    result = export_world(world_dir, tmp_path / "out")
    manifest = _manifest(result.directory)
    assert manifest["format"] == EXPORT_FORMAT
    assert result.directory.name == manifest["world_id"] == world_dir.name
    raw = (result.directory / "export.json").read_bytes()
    assert result.digest == hashlib.sha256(raw).hexdigest()
    # canonical JSON: re-serializing gives the same bytes.
    assert raw == json.dumps(
        json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    on_disk = {
        p.relative_to(result.directory).as_posix()
        for p in result.directory.rglob("*")
        if p.is_file() and p.name != "export.json"
    }
    listed = {f["path"]: f for f in manifest["files"]}
    assert set(listed) == on_disk
    for rel, entry in listed.items():
        data = (result.directory / rel).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), rel
        assert entry["bytes"] == len(data)
        assert b"\r" not in data, rel
        data.decode("utf-8")
        assert Path(rel).suffix in (".md", ".json", ".jsonl", ".csv"), rel


def test_export_has_no_binary_or_bookkeeping_and_renders_documents(
    world_dir: Path, tmp_path: Path
) -> None:
    result = export_world(world_dir, tmp_path / "out")
    files = {f["path"]: f for f in _manifest(result.directory)["files"]}
    assert not [p for p in files if p.startswith((".world", ".jobs", ".cache", "kg/neo4j"))]
    docx = sorted(p for p in world_dir.rglob("*.docx"))
    assert docx, "the fixture world renders at least one .docx"
    for source in docx:
        rel = source.relative_to(world_dir).as_posix()
        entry = files[f"{rel}.md"]
        assert entry["rendered_from"] == rel
        assert entry["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        text = (result.directory / f"{rel}.md").read_text(encoding="utf-8")
        assert text.startswith("# ")


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("kg/nodes.jsonl", "gold"),
        ("kg/provenance.jsonl", "gold"),
        ("evals/questions.jsonl", "gold"),
        ("validation/issues.jsonl", "gold"),
        ("config.json", "gold"),
        ("data/support_ops/spec.json", "gold"),
        ("data/support_ops/questions.json", "gold"),
        ("data/support_ops/lineage.json", "gold"),
        ("organization/company.md", "data"),
        ("data/support_ops/tables/dim_agent.csv", "data"),
    ],
)
def test_export_roles_keep_the_answer_key_gold(
    world_dir: Path, tmp_path: Path, path: str, role: str
) -> None:
    result = export_world(world_dir, tmp_path / "out")
    files = {f["path"]: f for f in _manifest(result.directory)["files"]}
    assert files[path]["role"] == role
    assert all(f["role"] == "data" for p, f in files.items() if p.startswith("artifacts/"))


def test_export_tables_carry_schema_and_keys(world_dir: Path, tmp_path: Path) -> None:
    result = export_world(world_dir, tmp_path / "out")
    tables = {t["name"]: t for t in _manifest(result.directory)["tables"]}
    dim = tables["support_ops.dim_agent"]
    fact = tables["support_ops.fact_agent_day"]
    assert dim["kind"] == "table" and dim["primary_key"] == ["agent_id"]
    assert fact["primary_key"] == ["agent_id", "date"]
    assert fact["foreign_keys"] == [
        {"columns": ["agent_id"], "table": "support_ops.dim_agent", "references": ["agent_id"]}
    ]
    for table in tables.values():
        lines = (result.directory / table["path"]).read_text(encoding="utf-8").splitlines()
        assert lines[0].split(",") == [c["name"] for c in table["columns"]]
        assert len(lines) > 1
        assert {c["type"] for c in table["columns"]} <= {
            "text",
            "integer",
            "decimal",
            "date",
            "timestamp",
        }
        if table["kind"] == "view":
            assert table["primary_key"] == [] and table["foreign_keys"] == []
    date_type = {c["name"]: c["type"] for c in fact["columns"]}["date"]
    assert date_type == "date"


def test_export_is_deterministic(world_dir: Path, tmp_path: Path) -> None:
    first = export_world(world_dir, tmp_path / "a")
    second = export_world(world_dir, tmp_path / "b")
    assert first.digest == second.digest
    for path in first.directory.rglob("*"):
        if path.is_file():
            twin = second.directory / path.relative_to(first.directory)
            assert path.read_bytes() == twin.read_bytes()


def test_export_refuses_what_it_cannot_export(world_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="not a world"):
        export_world(tmp_path, tmp_path / "out")
    export_world(world_dir, tmp_path / "out")
    with pytest.raises(ExportError, match="not empty"):
        export_world(world_dir, tmp_path / "out")
    (world_dir / "artifacts" / "blob.bin").write_bytes(b"\x00\x01")
    with pytest.raises(ExportError, match="no text rendering"):
        export_world(world_dir, tmp_path / "other")


def test_world_export_cli(
    world_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["world", "export", str(world_dir), "--out", str(tmp_path / "out")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert Path(printed["export_dir"]).name == world_dir.name
    assert main(["world", "export", str(tmp_path), "--out", str(tmp_path / "x")]) == 2


@pytest.mark.parametrize(
    ("backend", "prose"),
    [
        ("fake", "fake"),
        ("synth", "deterministic"),
        ("anthropic_api", "live"),
        ("bedrock", "live"),
        ("claude_cli", "live"),
    ],
)
def test_prose_backend_classification(backend: str, prose: str) -> None:
    assert prose_backend_of(backend) == prose
