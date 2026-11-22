"""``world export``: the text-only ``simenterprise-export/1`` directory."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import (
    EXPORT_FORMAT,
    ExportError,
    canonical_decimal,
    export_path,
    export_world,
    identity_shapes,
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
    assert manifest["simenterprise"]["repository"] == "SimEnterprise"
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
        (entry,) = [f for f in files.values() if f.get("rendered_from") == rel]
        assert entry["path"].endswith(".docx.md")
        assert entry["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        text = (result.directory / entry["path"]).read_text(encoding="utf-8")
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
        ("data/support_ops/spec.iteration-1.json", "gold"),
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
            "boolean",
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


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (1e-05, "0.00001"),
        (2.0, "2"),
        (-0.0, "0"),
        (3191.068163337267, "3191.068163"),
        (0.3415926535897932, "0.341593"),
        (12345678.123456789, "12345678.1235"),
        (0.0000004, "0"),
        (1.5e16, "15000000000000000"),
        (-12.50, "-12.5"),
        (0.1, "0.1"),
    ],
)
def test_decimals_are_fixed_notation(value: float, text: str) -> None:
    assert canonical_decimal(value) == text


@pytest.mark.parametrize(
    "rel",
    [
        "organization/company.md",
        "artifacts/" + "x" * 150 + "/" + "y" * 168 + ".docx.md",
        "artifacts/a/" + "z" * 120 + ".jira.json",
        "artifacts/a/name with spaces (1).md",
    ],
)
def test_export_paths_fit_the_package_limits(rel: str) -> None:
    out = export_path(rel)
    assert re.fullmatch(r"[A-Za-z0-9._/-]+", out)
    assert len(out.encode()) <= 160
    assert all(len(part.encode()) <= 90 for part in out.split("/"))
    assert out.endswith(Path(rel).suffix)
    assert export_path(rel) == out  # stable
    if rel == "organization/company.md":
        assert out == rel


def test_long_names_never_collide() -> None:
    a = export_path("artifacts/s/" + "n" * 120 + "-a.md")
    b = export_path("artifacts/s/" + "n" * 120 + "-b.md")
    assert a != b


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("write to someone@example.com today", "email address"),
        ("call +1 555-123-4567", "phone number"),
        ("ssn 123-45-6789", "SSN"),
        ("card 4111 1111 1111 1111", "card number"),
        ("card 4111111111111111", "card number"),
        ("0.4111111111111111", "card number"),
        ("card 3782-822463-10005", "card number"),
    ],
)
def test_identity_shapes_are_found(text: str, shape: str) -> None:
    assert shape in identity_shapes(text)


@pytest.mark.parametrize(
    "text",
    [
        "2026-01-05,0.3415926535897932,139",
        "agent-000004,person:esme-acosta,Esme Acosta,0.766691172642268",
        "sha256 9cc7203abeadae2184537a752ce3aa7487df840188246128e222eb42c4090257",
        "rows 12345678901234,7",
        "0.766691172642260",
        "2026-01-05T09:00:00",
    ],
)
def test_ordinary_values_are_not_identity_shapes(text: str) -> None:
    assert identity_shapes(text) == []


def test_export_refuses_an_identity_shaped_value(world_dir: Path, tmp_path: Path) -> None:
    (world_dir / "organization" / "contact.md").write_text("Mail ops@example.com\n")
    with pytest.raises(ExportError, match="email address"):
        export_world(world_dir, tmp_path / "out")


def test_table_cells_follow_the_package_rules(world_dir: Path, tmp_path: Path) -> None:
    result = export_world(world_dir, tmp_path / "out")
    tables = {t["name"]: t for t in _manifest(result.directory)["tables"]}
    types = {c["name"]: c["type"] for c in tables["support_ops.fact_ticket"]["columns"]}
    assert types["escalated"] == "boolean"
    weekly = {c["name"]: c["type"] for c in tables["support_ops.csat_weekly"]["columns"]}
    assert weekly["week"] == "date"
    for table in tables.values():
        lines = (result.directory / table["path"]).read_text(encoding="utf-8").splitlines()
        kinds = [c["type"] for c in table["columns"]]
        for line in lines[1:]:
            cells = line.split(",")
            if len(cells) != len(kinds):
                continue  # a quoted comma; checked by the csv round-trip below
            for cell, kind in zip(cells, kinds, strict=True):
                if not cell:
                    continue
                if kind == "decimal":
                    assert re.fullmatch(r"-?(0|[1-9]\d*)(\.\d{0,5}[1-9])?", cell), cell
                    assert len(re.sub(r"\D", "", cell)) <= 12, cell
                elif kind == "integer":
                    assert re.fullmatch(r"-?(0|[1-9]\d*)", cell), cell
                elif kind == "date":
                    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cell), cell
                elif kind == "timestamp":
                    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cell), cell
                elif kind == "boolean":
                    assert cell in ("true", "false")
        with (result.directory / table["path"]).open(newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        assert all(len(row) == len(kinds) for row in rows)


def test_world_profile_summarizes_a_world(world_dir: Path) -> None:
    from enterprise_sim.worlds.profile import profile_world

    profile = profile_world(world_dir)
    assert profile["world_id"] == world_dir.name
    assert profile["org"]["people"] > 0 and profile["org"]["departments"]
    assert profile["documents"]["artifacts"] > 0
    assert profile["documents"]["authored_share"] == 1.0
    assert set(profile["data"]) == {"support_ops"}
    assert profile["data"]["support_ops"]["rows"] > 0
    assert profile["kg"]["nodes"] > 0


def test_world_profile_cli(world_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["world", "profile", str(world_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["world_id"] == world_dir.name


@pytest.mark.parametrize(
    ("text", "media", "found"),
    [
        ('{"canonical": "v2.3", "value": "x"}', "application/json", []),
        ('{"token": "v2.3"}', "application/json", ["token"]),
        ('{"props": {"api_key": "x"}}\n', "application/x-ndjson", ["api_key"]),
        ("attribute,version,canonical\n", "text/csv", []),
        ("id,api_token\n", "text/csv", ["api_token"]),
        ("a token in prose", "text/markdown", []),
        (
            '{"authoring": "x", "est_input_tokens_per_artifact": 1, "key": "BSD-1"}',
            "application/json",
            [],
        ),
        (
            '{"auth_token": "x", "session_secret": "y"}',
            "application/json",
            ["auth_token", "session_secret"],
        ),
    ],
)
def test_secret_like_names_are_found(text: str, media: str, found: list[str]) -> None:
    from enterprise_sim.exporters.text_export import secret_names

    assert secret_names(text, media) == found


@pytest.mark.parametrize(
    ("model", "accepts"),
    [
        ("claude-haiku-4-5", True),
        ("us.anthropic.claude-haiku-5-5", False),
        ("claude-opus-5-5", False),
        ("claude-opus-4-8", False),
        ("claude-sonnet-4-6", True),
    ],
)
def test_current_models_get_no_temperature(model: str, accepts: bool) -> None:
    from enterprise_sim.core.llm.backends import accepts_temperature

    assert accepts_temperature(model) is accepts


def test_bedrock_ids_are_priced_as_their_model() -> None:
    from enterprise_sim.core.llm.pricing import PRICING, pricing_for

    assert pricing_for("us.anthropic.claude-haiku-5-5") == PRICING["claude-haiku-5-5"]
