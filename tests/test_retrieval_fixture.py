"""``world retrieval-fixture``: a judged retrieval corpus from an export, pinned by digest."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import main
from enterprise_sim.exporters.text_export import export_world
from enterprise_sim.facts.catalog import value_token
from enterprise_sim.worlds.retrieval_fixture import build_fixture

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "worlds"


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("retrieval")
    text = (_EXAMPLES / "coding-team-messy.toml").read_text(encoding="utf-8")
    (root / "cfg.toml").write_text(re.sub(r"(?m)^worlds_root = .*$", "", text), encoding="utf-8")
    assert main(["world", "create", str(root / "cfg.toml"), "--worlds-root", str(root / "w")]) == 0
    (world,) = list((root / "w").iterdir())
    return export_world(world, root / "out").directory


def _bundle(export: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    files = build_fixture(export)
    corpus = [json.loads(line) for line in files["corpus.jsonl"].decode().splitlines()]
    queries = [json.loads(line) for line in files["queries.jsonl"].decode().splitlines()]
    return corpus, queries, json.loads(files["manifest.json"])


def test_keys_are_unique_and_every_reference_resolves(export: Path) -> None:
    corpus, queries, _ = _bundle(export)
    keys = [r["key"] for r in corpus]
    assert len(keys) == len(set(keys))
    known = set(keys)
    for record in corpus:
        if record["kind"] == "fact":
            assert set(record["cites"]) <= known
            assert record.get("replaces") is None or record["replaces"] in known
    for query in queries:
        assert any(g == 2 for g in query["answers"].values())
        assert (
            set(query["answers"]) | set(query.get("served", [])) | set(query.get("not_served", []))
            <= known
        )


def test_a_document_graded_2_states_the_value(export: Path) -> None:
    corpus, queries, _ = _bundle(export)
    text = {r["key"]: r["text"] for r in corpus}
    value_of = {
        r["key"]: r["text"].rsplit(" is ", 1)[1].rstrip(".") for r in corpus if r["kind"] == "fact"
    }
    checked = 0
    for query in queries:
        (fact,) = [k for k, g in query["answers"].items() if g == 2 and k.startswith("fact:")]
        token = value_token(value_of[fact])
        for key, grade in query["answers"].items():
            if grade == 2 and key.startswith("doc:"):
                assert token in text[key], (query["id"], key)
                checked += 1
    assert checked > 0


def test_as_of_queries_serve_the_older_version_inside_its_interval(export: Path) -> None:
    corpus, queries, _ = _bundle(export)
    facts = {r["key"]: r for r in corpus if r["kind"] == "fact"}
    as_of = [q for q in queries if "as_of" in q]
    assert as_of
    for query in as_of:
        (older,) = query["served"]
        (newer,) = query["not_served"]
        assert facts[newer]["replaces"] == older
        at = datetime.fromisoformat(query["as_of"].replace("Z", ""))
        assert datetime.fromisoformat(facts[older]["valid_from"].replace("Z", "")) < at
        assert at < datetime.fromisoformat(facts[newer]["valid_from"].replace("Z", ""))
        assert query["answers"][older] == 2


def test_the_manifest_pins_both_files_and_the_export(export: Path) -> None:
    files = build_fixture(export)
    manifest = json.loads(files["manifest.json"])
    assert (
        manifest["export_digest"]
        == hashlib.sha256((export / "export.json").read_bytes()).hexdigest()
    )
    for name in ("corpus.jsonl", "queries.jsonl"):
        assert manifest["files"][name] == hashlib.sha256(files[name]).hexdigest()
    assert build_fixture(export) == files  # deterministic


def test_the_cli_writes_the_bundle(
    export: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["world", "retrieval-fixture", str(export), "--out", str(tmp_path / "b")]) == 0
    assert json.loads(capsys.readouterr().out)["world_id"] == export.name
    assert sorted(p.name for p in (tmp_path / "b").iterdir()) == [
        "corpus.jsonl",
        "manifest.json",
        "queries.jsonl",
    ]
    assert main(["world", "retrieval-fixture", str(tmp_path), "--out", str(tmp_path / "x")]) == 2
