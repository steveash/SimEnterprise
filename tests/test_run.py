"""Build-pipeline tests: the world directory layout, ``.world``, and reproducibility (M1)."""

from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from enterprise_sim.assembly import (
    build_world_file,
    compute_config_digest,
    compute_world_id,
    execute_run,
)
from enterprise_sim.cli import main
from enterprise_sim.core.config import WorldConfig, load_config_from_mapping
from enterprise_sim.worlds import (
    WORLD_FILE,
    WorldFile,
    is_world_dir,
    read_world,
    structural_view,
)


def _config(worlds_root: Path, *, seed: int = 7, name: str = "Acme Corp") -> WorldConfig:
    return load_config_from_mapping(
        {
            "company": {"name": name, "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "worlds_root": str(worlds_root),
        }
    )


def test_execute_run_lays_out_directory(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path / "out"))

    assert result.world_dir == tmp_path / "out" / result.world_id
    assert result.world_dir.is_dir()
    assert is_world_dir(result.world_dir)
    assert (result.world_dir / WORLD_FILE).is_file()
    for sub in ("organization", "kg", "validation"):
        assert (result.world_dir / sub).is_dir()
    # The old scattered state files are gone: one folder, one state file.
    assert not (result.world_dir / "manifest.json").exists()
    assert not (result.world_dir / "config.snapshot.json").exists()


def test_response_cache_lives_inside_the_world(tmp_path: Path) -> None:
    # D31 + portability: with no explicit scale.cache_dir the cache is world-local,
    # so a zipped world carries everything a resume needs.
    result = execute_run(_config(tmp_path))
    assert (result.world_dir / ".cache" / "llm").is_dir()
    assert result.world_file.outputs["cache"] == ".cache/llm/"


def test_organization_and_kg_are_populated(tmp_path: Path) -> None:
    # Layer A (M4) fills organization/ with markdown reference data and kg/ with
    # the structural export; the world is no longer empty.
    result = execute_run(_config(tmp_path))
    org = result.world_dir / "organization"
    assert (org / "README.md").is_file()
    assert (org / "company.md").is_file()
    assert (org / "people.md").is_file()
    assert (org / "departments").is_dir()
    assert (result.world_dir / "kg" / "nodes.jsonl").is_file()
    assert (result.world_dir / "kg" / "edges.jsonl").is_file()
    assert result.world.node_count > 0


def test_run_produces_a_full_markdown_corpus(tmp_path: Path) -> None:
    # M6/M8 end to end: world -> scheduler events -> producers -> mixed corpus
    # (markdown by default; .docx for the word-bound document kinds).
    result = execute_run(_config(tmp_path))

    # The simulation produced a non-trivial event log and a corpus of files.
    assert len(result.corpus.journal) > 0
    assert len(result.corpus.artifacts) > 0

    # Every rendered artifact landed on disk: markdown, docx, or a jira issue.
    art_files = [p for p in (result.world_dir / "artifacts").rglob("*") if p.is_file()]
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
    events = (result.world_dir / "kg" / "events.jsonl").read_text().splitlines()
    assert len(events) == len(result.corpus.journal)
    assert (result.world_dir / "kg" / "mentions.jsonl").is_file()
    assert (result.world_dir / "kg" / "provenance.jsonl").is_file()
    assert (result.world_dir / "validation" / "issues.jsonl").is_file()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_mentions_and_provenance_verify_against_the_corpus(tmp_path: Path) -> None:
    # M7 acceptance (esim-8fe769ce): mentions.jsonl + provenance.jsonl are
    # populated and every record is verifiable against the actual corpus —
    # mentions point at the exact rendered text, provenance at real KG targets.
    result = execute_run(_config(tmp_path))
    world_dir = result.world_dir

    node_ids = {n["id"] for n in _read_jsonl(world_dir / "kg" / "nodes.jsonl")}
    edge_ids = {e["id"] for e in _read_jsonl(world_dir / "kg" / "edges.jsonl")}
    targetable = node_ids | edge_ids
    corpus_paths = {a.path for a in result.corpus.artifacts}

    mentions = _read_jsonl(world_dir / "kg" / "mentions.jsonl")
    provenance = _read_jsonl(world_dir / "kg" / "provenance.jsonl")
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
            assert (world_dir / art["path"]).is_file()
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
        world_dir = result.world_dir  # type: ignore[attr-defined]
        out: dict[str, bytes] = {}
        for sub in ("artifacts", "kg", "validation"):
            for path in sorted((world_dir / sub).rglob("*")):
                if path.is_file():
                    # Read bytes so binary (.docx) artifacts compare byte-for-byte too.
                    out[str(path.relative_to(world_dir))] = path.read_bytes()
        return out

    assert a.world_id == b.world_id
    assert corpus_blob(a) == corpus_blob(b)


def test_eval_reads_the_worlds_event_log(tmp_path: Path) -> None:
    # The wired build writes kg/events.jsonl, which the eval subcommand consumes.
    result = execute_run(_config(tmp_path))
    assert main(["eval", str(result.world_dir)]) == 0


def test_world_id_is_slug_plus_digest(tmp_path: Path) -> None:
    config = _config(tmp_path, name="Acme Corp")
    result = execute_run(config)
    assert result.world_id.startswith("acme-corp-")
    assert result.world_id == compute_world_id(config)


def test_world_file_round_trips_and_matches_disk(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path))
    on_disk = json.loads((result.world_dir / WORLD_FILE).read_text())

    assert WorldFile.from_dict(on_disk) == result.world_file
    assert on_disk == result.world_file.to_dict()
    assert on_disk["schema_version"] == "2.0"
    assert on_disk["world_id"] == result.world_id
    # Counts mirror the built world (nodes/edges) and the simulated event journal.
    assert on_disk["counts"] == {
        "nodes": result.world.node_count,
        "edges": result.world.edge_count,
        "events": len(result.corpus.journal),
    }
    assert on_disk["counts"]["nodes"] > 0
    assert on_disk["counts"]["events"] > 0
    assert on_disk["seed"] == 7
    assert on_disk["render_backend"] == "fake"
    assert on_disk["history"][0]["kind"] == "create"
    assert on_disk["lineage"] is None


def test_bare_build_closes_out_the_job_ledger(tmp_path: Path) -> None:
    # A build with no job layer above it is still a complete job as far as any
    # consumer (the explorer's world list) is concerned.
    result = execute_run(_config(tmp_path))
    job = result.world_file.job
    assert job.status == "done"
    assert job.phase == "done"
    assert job.artifacts_done == job.artifacts_total == len(result.corpus.artifacts)
    assert job.finished_at is not None


def test_world_file_carries_the_effective_config_minus_its_destination(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = execute_run(config)
    recorded = result.world_file.config
    # The destination is not part of the world: a .world stays valid after the
    # folder is zipped and moved.
    assert "worlds_root" not in recorded
    default_dir = WorldConfig.model_fields["worlds_root"].default
    portable = config.model_copy(update={"worlds_root": default_dir})
    assert WorldConfig.model_validate(recorded) == portable
    on_disk = read_world(result.world_dir)
    assert on_disk is not None
    assert WorldConfig.model_validate(on_disk.config) == portable


def test_same_seed_reproduces_structural_world_file(tmp_path: Path) -> None:
    # Two builds of the same config to *different* destinations must share a world
    # id and an identical structural .world (PLAN.md M1 acceptance).
    a = execute_run(_config(tmp_path / "a", seed=42))
    b = execute_run(_config(tmp_path / "b", seed=42))

    assert a.world_id == b.world_id
    assert structural_view(a.world_file.to_dict()) == structural_view(b.world_file.to_dict())


def test_different_seed_changes_world_id(tmp_path: Path) -> None:
    a = execute_run(_config(tmp_path / "a", seed=1))
    b = execute_run(_config(tmp_path / "b", seed=2))
    assert a.world_id != b.world_id
    assert a.world_file.config_digest != b.world_file.config_digest


def test_digest_excludes_worlds_root(tmp_path: Path) -> None:
    # Where a world lands is not part of its identity.
    here = _config(tmp_path / "here")
    there = _config(tmp_path / "there")
    assert compute_config_digest(here) == compute_config_digest(there)
    assert compute_world_id(here) == compute_world_id(there)


def test_timestamps_are_the_only_volatile_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)
    m1 = build_world_file(config, generated_at="2026-06-22T00:00:00+00:00")
    m2 = build_world_file(config, generated_at="2026-06-22T12:00:00+00:00")
    assert m1 != m2
    assert structural_view(m1.to_dict()) == structural_view(m2.to_dict())


def test_execute_run_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = execute_run(config, generated_at="2026-06-22T00:00:00+00:00")
    second = execute_run(config, generated_at="2026-06-22T00:00:00+00:00")
    assert first.world_dir == second.world_dir
    assert (first.world_dir / WORLD_FILE).read_text() == (second.world_dir / WORLD_FILE).read_text()
    # A same-config rebuild is not an extend: the history does not grow.
    assert [h["kind"] for h in second.world_file.history] == ["create"]


def test_extending_in_place_keeps_the_world_id_and_logs_history(tmp_path: Path) -> None:
    # An extend re-renders into the SAME directory with a later period_end: the
    # id is minted once and the .world records what changed.
    config = _config(tmp_path)
    first = execute_run(config)
    longer = config.model_copy(
        update={
            "simulation": config.simulation.model_copy(update={"period_end": date(2026, 2, 28)})
        }
    )
    assert compute_world_id(longer) != first.world_id  # a fresh build would get a new id...

    second = execute_run(longer, world_dir=first.world_dir)
    assert second.world_dir == first.world_dir
    assert second.world_id == first.world_id  # ...but in place it keeps the minted one
    assert second.world_file.window["end"] == "2026-02-28"
    assert second.world_file.config_digest == compute_config_digest(longer)
    assert [h["kind"] for h in second.world_file.history] == ["create", "extend"]
    assert second.world_file.history[-1]["changes"]["config_digest"] == [
        first.world_file.config_digest,
        second.world_file.config_digest,
    ]
    assert len(second.corpus.journal) > len(first.corpus.journal)


def test_cli_world_create_writes_outputs(tmp_path: Path) -> None:
    cfg_path = tmp_path / "demo.toml"
    cfg_path.write_text(
        "seed = 3\n"
        '[company]\nname = "Acme"\nvertical = "software"\nsize = "small"\n'
        "[simulation]\nperiod_start = 2026-01-01\nperiod_end = 2026-01-31\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert main(["world", "create", "-c", str(cfg_path), "--worlds-root", str(out)]) == 0

    world_dirs = list(out.iterdir())
    assert len(world_dirs) == 1
    assert is_world_dir(world_dirs[0])


def test_cli_world_create_requires_config() -> None:
    assert main(["world", "create"]) == 2
