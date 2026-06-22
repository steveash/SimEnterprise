"""The v1 *golden run* acceptance test (esim-3481176c, PLAN.md §4).

This is the end-to-end acceptance for the first vertical slice: the committed
``examples/golden.toml`` config — one department, one scenario, a small team,
over a single business week — must produce a markdown corpus *and* a gold
knowledge graph that:

* has the documented shape (1 department / 1 scenario / a non-trivial team / a
  one-week window),
* **validates as the answer key** — every provenance edge resolves to a real KG
  target and a real corpus artifact, every mention's locator slices the rendered
  text to exactly its surface form, and the run carries *no hard* consistency
  failure (dangling references, scheduling conflicts, out-of-window stamps); only
  soft, report-and-continue grounding findings are permitted (D17/D30), and
* **reproduces byte-for-byte** from its seed (D10/D26/D31, ``fake`` backend).

If this test fails, the documented golden run in ``docs/GOLDEN_RUN.md`` is no
longer accurate — update one or the other together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.assembly import execute_run
from enterprise_sim.assembly.validation import (
    DANGLING_EDGE_ENDPOINT,
    DANGLING_EVENT_REFERENCE,
    OUT_OF_WINDOW,
    SCHEDULING_CONFLICT,
)
from enterprise_sim.core.config import RunConfig, load_config

# The consistency checks whose findings would mean the gold KG is *not* a trustworthy
# answer key. Soft grounding findings (e.g. ``unresolved_mention``) are expected and
# accounted (D17/D30), so they are deliberately not in this set.
_HARD_ISSUE_KINDS = frozenset(
    {
        DANGLING_EDGE_ENDPOINT,
        DANGLING_EVENT_REFERENCE,
        SCHEDULING_CONFLICT,
        OUT_OF_WINDOW,
    }
)

_GOLDEN_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "golden.toml"


def _golden_config(output_dir: Path) -> RunConfig:
    """Load the committed golden config, redirected to ``output_dir``."""
    config = load_config(_GOLDEN_CONFIG)
    return config.model_copy(update={"output_dir": output_dir})


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_golden_config_loads_and_is_tiny() -> None:
    # The committed config is the documented "first vertical slice": a startup
    # over a single Mon–Fri business week. Guards docs/GOLDEN_RUN.md.
    config = load_config(_GOLDEN_CONFIG)
    assert config.company.size.value == "startup"
    assert config.simulation.period_start.isoformat() == "2026-01-05"
    assert config.simulation.period_end.isoformat() == "2026-01-09"
    assert config.projects == ()  # one scenario comes from the archetype's playbook


def test_golden_run_has_the_documented_shape(tmp_path: Path) -> None:
    result = execute_run(_golden_config(tmp_path))
    nodes = _read_jsonl(result.run_dir / "kg" / "nodes.jsonl")

    departments = [n for n in nodes if n["type"] == "Department"]
    scenarios = [
        n for n in nodes if n["type"] == "Initiative" and n["props"].get("type") == "scenario"
    ]
    people = [n for n in nodes if n["type"] == "Person"]

    # 1 department / 1 scenario / a small (non-trivial) team — the slice's shape.
    assert len(departments) == 1
    assert len(scenarios) == 1
    assert len(people) >= 3

    # A one-week window and a non-empty corpus + journal.
    assert result.manifest.window == {"start": "2026-01-05", "end": "2026-01-09"}
    assert result.corpus.artifacts
    assert result.corpus.journal
    md_files = list((result.run_dir / "artifacts").rglob("*.md"))
    assert len(md_files) == len(result.corpus.artifacts)


def test_golden_kg_validates_as_the_answer_key(tmp_path: Path) -> None:
    result = execute_run(_golden_config(tmp_path))
    run_dir = result.run_dir

    node_ids = {n["id"] for n in _read_jsonl(run_dir / "kg" / "nodes.jsonl")}
    edge_ids = {e["id"] for e in _read_jsonl(run_dir / "kg" / "edges.jsonl")}
    targetable = node_ids | edge_ids
    corpus_paths = {a.path for a in result.corpus.artifacts}

    issues = _read_jsonl(run_dir / "validation" / "issues.jsonl")

    # (1) No *hard* inconsistency: the answer key is internally sound. Any soft
    #     grounding finding is allowed but must be report-and-continue (D17/D30).
    hard = [row for row in issues if row.get("kind") in _HARD_ISSUE_KINDS]
    assert hard == [], f"golden run has hard consistency failures: {hard}"

    # (2) The manifest's validation summary is a faithful index of issues.jsonl.
    by_kind: dict[str, int] = {}
    for row in issues:
        by_kind[str(row["kind"])] = by_kind.get(str(row["kind"]), 0) + 1
    assert result.manifest.validation["total"] == len(issues)
    assert result.manifest.validation["by_kind"] == {k: by_kind[k] for k in sorted(by_kind)}

    # (3) Every provenance edge targets a real node/edge and cites real artifacts;
    #     provenance covers both nodes and edges (not just entities).
    provenance = _read_jsonl(run_dir / "kg" / "provenance.jsonl")
    assert provenance, "expected a populated provenance.jsonl"
    for row in provenance:
        assert row["target_id"] in targetable, f"dangling provenance {row['target_id']}"
        assert row["artifacts"], "provenance with no supporting artifact"
        for art in row["artifacts"]:
            assert art["path"] in corpus_paths
            assert (run_dir / art["path"]).is_file()
    prov_targets = {row["target_id"] for row in provenance}
    assert prov_targets & node_ids, "expected node-targeted provenance"
    assert prov_targets & edge_ids, "expected edge-targeted provenance"

    # (4) Every mention's locator slices the rendered file to exactly its surface
    #     form, on the line it claims, and resolves to a real entity (D20).
    mentions = _read_jsonl(run_dir / "kg" / "mentions.jsonl")
    assert mentions, "expected a populated mentions.jsonl"
    bodies: dict[str, str] = {}
    for m in mentions:
        path = m["artifact_path"]
        assert path in corpus_paths, f"mention in unknown artifact {path}"
        body = bodies.setdefault(path, (run_dir / path).read_text(encoding="utf-8"))
        loc = m["locator"]
        span = body[loc["offset"] : loc["offset"] + loc["length"]]
        assert span == m["surface_form"], f"locator mismatch in {path}"
        assert loc["line"] == body[: loc["offset"]].count("\n") + 1
        assert m["entity_id"] in node_ids, f"mention of unknown entity {m['entity_id']}"


def test_golden_kg_exports_every_canonical_artifact(tmp_path: Path) -> None:
    # The answer key is emitted in full: the canonical JSONL set, the schema +
    # whole-graph snapshot, and the Neo4j/Cypher adapter (§11.4-11.5, D19).
    result = execute_run(_golden_config(tmp_path))
    kg = result.run_dir / "kg"
    for name in (
        "nodes.jsonl",
        "edges.jsonl",
        "events.jsonl",
        "provenance.jsonl",
        "mentions.jsonl",
        "aliases.jsonl",
        "schema.json",
        "graph.json",
    ):
        assert (kg / name).is_file(), f"missing canonical KG file {name}"
    assert (kg / "neo4j" / "import.cypher").is_file()


def test_golden_run_reproduces_byte_for_byte(tmp_path: Path) -> None:
    # Acceptance: the golden corpus + gold KG reproduce from the seed (D10/D26/D31).
    a = execute_run(_golden_config(tmp_path / "a"))
    b = execute_run(_golden_config(tmp_path / "b"))

    def blob(run_dir: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for sub in ("artifacts", "kg", "validation", "organization"):
            for path in sorted((run_dir / sub).rglob("*")):
                if path.is_file():
                    out[str(path.relative_to(run_dir))] = path.read_text(encoding="utf-8")
        return out

    assert a.run_id == b.run_id
    assert blob(a.run_dir) == blob(b.run_dir)


@pytest.mark.parametrize("expected_run_id", ["golden-slice-co-40644d551158"])
def test_golden_run_id_is_pinned(tmp_path: Path, expected_run_id: str) -> None:
    # The run id is a pure function of (config, seed); pinning it makes an
    # accidental change to the golden config a loud, reviewable test failure.
    result = execute_run(_golden_config(tmp_path))
    assert result.run_id == expected_run_id
