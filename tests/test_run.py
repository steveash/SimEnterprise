"""Run-skeleton tests: directory layout, manifest, and reproducibility (M1)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from enterprise_sim.assembly import (
    Manifest,
    build_manifest,
    compute_config_digest,
    compute_run_id,
    execute_run,
    structural_view,
)
from enterprise_sim.cli import main
from enterprise_sim.core.config import RunConfig, load_config_from_mapping


def _config(output_dir: Path, *, seed: int = 7, name: str = "Acme Corp") -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": name, "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "output_dir": str(output_dir),
        }
    )


def test_execute_run_lays_out_directory(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path / "out"))

    assert result.run_dir == tmp_path / "out" / result.run_id
    assert result.run_dir.is_dir()
    assert (result.run_dir / "manifest.json").is_file()
    assert (result.run_dir / "config.snapshot.json").is_file()
    for sub in ("organization", "kg", "validation"):
        assert (result.run_dir / sub).is_dir()


def test_organization_and_kg_are_populated(tmp_path: Path) -> None:
    # Layer A (M4) fills organization/ with markdown reference data and kg/ with
    # the structural export; the run is no longer empty.
    result = execute_run(_config(tmp_path))
    org = result.run_dir / "organization"
    assert (org / "README.md").is_file()
    assert (org / "company.md").is_file()
    assert (org / "people.md").is_file()
    assert (org / "departments").is_dir()
    assert (result.run_dir / "kg" / "nodes.jsonl").is_file()
    assert (result.run_dir / "kg" / "edges.jsonl").is_file()
    assert result.world.node_count > 0


def test_run_produces_a_full_markdown_corpus(tmp_path: Path) -> None:
    # M6/M8 end to end: world -> scheduler events -> producers -> mixed corpus
    # (markdown by default; .docx for the word-bound document kinds).
    result = execute_run(_config(tmp_path))

    # The simulation produced a non-trivial event log and a corpus of files.
    assert len(result.corpus.journal) > 0
    assert len(result.corpus.artifacts) > 0

    # Every rendered artifact landed on disk: markdown, docx, or a jira issue.
    art_files = [p for p in (result.run_dir / "artifacts").rglob("*") if p.is_file()]
    assert len(art_files) == len(result.corpus.artifacts)
    # Markdown files are non-empty with a templated H1 title; docx files are valid
    # OOXML zip packages carrying the main document part; jira files are valid Jira
    # issue JSON (the multi-modal fan-out, D6) with a key + fields.
    for path in art_files:
        if path.suffix == ".md":
            assert path.read_text(encoding="utf-8").startswith("# ")
        elif path.name.endswith(".jira.json"):
            issue = json.loads(path.read_text(encoding="utf-8"))
            assert issue["key"] and issue["fields"]["summary"]
        else:
            assert path.suffix == ".docx"
            with zipfile.ZipFile(path) as zf:
                assert "word/document.xml" in zf.namelist()

    # The Layer B/C side files are all written and line-count consistent.
    events = (result.run_dir / "kg" / "events.jsonl").read_text().splitlines()
    assert len(events) == len(result.corpus.journal)
    assert (result.run_dir / "kg" / "mentions.jsonl").is_file()
    assert (result.run_dir / "kg" / "provenance.jsonl").is_file()
    assert (result.run_dir / "validation" / "issues.jsonl").is_file()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_mentions_and_provenance_verify_against_the_corpus(tmp_path: Path) -> None:
    # M7 acceptance (esim-8fe769ce): mentions.jsonl + provenance.jsonl are
    # populated and every record is verifiable against the actual corpus —
    # mentions point at the exact rendered text, provenance at real KG targets.
    result = execute_run(_config(tmp_path))
    run_dir = result.run_dir

    node_ids = {n["id"] for n in _read_jsonl(run_dir / "kg" / "nodes.jsonl")}
    edge_ids = {e["id"] for e in _read_jsonl(run_dir / "kg" / "edges.jsonl")}
    targetable = node_ids | edge_ids
    corpus_paths = {a.path for a in result.corpus.artifacts}

    mentions = _read_jsonl(run_dir / "kg" / "mentions.jsonl")
    provenance = _read_jsonl(run_dir / "kg" / "provenance.jsonl")
    assert mentions, "expected a populated mentions.jsonl"
    assert provenance, "expected a populated provenance.jsonl"

    # Each mention's locator must slice the rendered text to exactly its surface
    # form, on the line it claims, and resolve to a real entity node (D20). For a
    # binary artifact (.docx) the rendered *text* is the producer's plain-text
    # projection (``artifact.body``) — what the tagger ran over — not the on-disk
    # bytes, so we verify locators against that.
    bodies = {a.path: a.body for a in result.corpus.artifacts}
    for m in mentions:
        path = m["artifact_path"]
        assert path in corpus_paths, f"mention in unknown artifact {path}"
        body = bodies[path]
        loc = m["locator"]
        span = body[loc["offset"] : loc["offset"] + loc["length"]]
        assert span == m["surface_form"], f"locator mismatch in {path}"
        assert loc["line"] == body[: loc["offset"]].count("\n") + 1
        assert m["entity_id"] in node_ids, f"mention of unknown entity {m['entity_id']}"

    # Every provenance target is a real node or edge, and each cited artifact is a
    # real corpus file. The locator field is reserved (omitted) in v1 (D19).
    for row in provenance:
        assert row["target_id"] in targetable, f"dangling provenance {row['target_id']}"
        assert row["artifacts"], "provenance with no supporting artifact"
        for art in row["artifacts"]:
            assert art["path"] in corpus_paths
            assert (run_dir / art["path"]).is_file()
            assert "locator" not in art  # artifact-level in v1; span locator reserved

    # The artifact node and every reified edge it expresses appear as targets, so
    # provenance covers both nodes and edges, not just entities.
    prov_targets = {row["target_id"] for row in provenance}
    assert prov_targets & node_ids, "expected node-targeted provenance"
    assert prov_targets & edge_ids, "expected edge-targeted provenance"


def test_corpus_is_clustered_by_scenario(tmp_path: Path) -> None:
    # D29 cache locality: a scenario's artifacts share a directory prefix.
    result = execute_run(_config(tmp_path))
    rels = [Path(a.path) for a in result.corpus.artifacts]
    assert rels, "expected a non-empty corpus"
    # Every artifact lives two levels deep under a per-scenario cluster dir.
    assert all(r.parts[0] == "artifacts" for r in rels)
    assert all(len(r.parts) == 3 for r in rels)
    # All from this small (single-scenario) config share one cluster.
    assert len({r.parts[1] for r in rels}) == 1


def test_corpus_reproduces_byte_for_byte_by_seed(tmp_path: Path) -> None:
    # Acceptance: a full corpus reproducible by seed (D10/D26/D31, fake backend).
    a = execute_run(_config(tmp_path / "a", seed=11))
    b = execute_run(_config(tmp_path / "b", seed=11))

    def corpus_blob(result: object) -> dict[str, bytes]:
        run_dir = result.run_dir  # type: ignore[attr-defined]
        out: dict[str, bytes] = {}
        for sub in ("artifacts", "kg", "validation"):
            for path in sorted((run_dir / sub).rglob("*")):
                if path.is_file():
                    # Read bytes so binary (.docx) artifacts compare byte-for-byte too.
                    out[str(path.relative_to(run_dir))] = path.read_bytes()
        return out

    assert a.run_id == b.run_id
    assert corpus_blob(a) == corpus_blob(b)


def test_eval_reads_the_runs_event_log(tmp_path: Path) -> None:
    # The wired run writes kg/events.jsonl, which the eval subcommand consumes.
    result = execute_run(_config(tmp_path))
    assert main(["eval", str(result.run_dir)]) == 0


def test_run_id_is_slug_plus_digest(tmp_path: Path) -> None:
    config = _config(tmp_path, name="Acme Corp")
    result = execute_run(config)
    assert result.run_id.startswith("acme-corp-")
    assert result.run_id == compute_run_id(config)


def test_manifest_round_trips_and_matches_disk(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path))
    on_disk = json.loads((result.run_dir / "manifest.json").read_text())

    assert Manifest.from_dict(on_disk) == result.manifest
    assert on_disk == result.manifest.to_dict()
    # Counts mirror the built world (nodes/edges) and the simulated event journal.
    assert on_disk["counts"] == {
        "nodes": result.world.node_count,
        "edges": result.world.edge_count,
        "events": len(result.corpus.journal),
    }
    assert on_disk["counts"]["nodes"] > 0
    assert on_disk["counts"]["events"] > 0
    assert on_disk["seed"] == 7


def test_config_snapshot_round_trips(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = execute_run(config)
    snapshot = json.loads((result.run_dir / "config.snapshot.json").read_text())
    assert RunConfig.model_validate(snapshot) == config


def test_same_seed_reproduces_structural_manifest(tmp_path: Path) -> None:
    # Two runs of the same config to *different* destinations must share a run id
    # and an identical structural manifest (PLAN.md M1 acceptance).
    a = execute_run(_config(tmp_path / "a", seed=42))
    b = execute_run(_config(tmp_path / "b", seed=42))

    assert a.run_id == b.run_id
    assert structural_view(a.manifest.to_dict()) == structural_view(b.manifest.to_dict())


def test_different_seed_changes_run_id(tmp_path: Path) -> None:
    a = execute_run(_config(tmp_path / "a", seed=1))
    b = execute_run(_config(tmp_path / "b", seed=2))
    assert a.run_id != b.run_id
    assert a.manifest.config_digest != b.manifest.config_digest


def test_digest_excludes_output_dir(tmp_path: Path) -> None:
    # Where a run lands is not part of its identity.
    here = _config(tmp_path / "here")
    there = _config(tmp_path / "there")
    assert compute_config_digest(here) == compute_config_digest(there)
    assert compute_run_id(here) == compute_run_id(there)


def test_generated_at_is_the_only_volatile_field(tmp_path: Path) -> None:
    config = _config(tmp_path)
    m1 = build_manifest(config, generated_at="2026-06-22T00:00:00+00:00")
    m2 = build_manifest(config, generated_at="2026-06-22T12:00:00+00:00")
    assert m1 != m2
    assert structural_view(m1.to_dict()) == structural_view(m2.to_dict())


def test_execute_run_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = execute_run(config, generated_at="2026-06-22T00:00:00+00:00")
    second = execute_run(config, generated_at="2026-06-22T00:00:00+00:00")
    assert first.run_dir == second.run_dir
    assert (first.run_dir / "manifest.json").read_text() == (
        second.run_dir / "manifest.json"
    ).read_text()


def test_cli_run_writes_outputs(tmp_path: Path) -> None:
    cfg_path = tmp_path / "demo.toml"
    cfg_path.write_text(
        "seed = 3\n"
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-01\nperiod_end = 2026-01-31\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert main(["run", "-c", str(cfg_path), "-o", str(out)]) == 0

    run_dirs = list(out.iterdir())
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "manifest.json").is_file()


def test_cli_run_requires_config() -> None:
    assert main(["run"]) == 2
