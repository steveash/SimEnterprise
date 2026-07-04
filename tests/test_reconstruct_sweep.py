"""Edge-confidence threshold sweep tests (esim-ecr.3).

Covers the sweep harness the acceptance criteria name:

* **Build-once, re-threshold-many** — one :class:`PipelineExtraction` is aggregated
  at many thresholds via :meth:`PipelineExtraction.build`, so the sweep never
  re-extracts (no LLM per threshold).
* **Curve shape** — on a hand-built fixture with a true edge (high confidence) and a
  false edge (low confidence), raising the threshold climbs edge precision to the F1
  sweet spot; recall and the kept-edge count are monotonically non-increasing, and
  node metrics are invariant (the threshold gates edges only).
* **Keyless end to end** — the sweep runs over a fresh golden-run reconstruction via
  the fake backend with no key, and a threshold above the max confidence gates every
  edge away.
* **CLI** — ``reconstruct sweep`` is registered, parses ``--thresholds``, and writes
  a loadable table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.llm import LLMConfig, build_client
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.reconstruct import (
    CandidateTriple,
    Chunk,
    Extraction,
    MentionSpan,
    PipelineExtraction,
    Resolution,
    extract_once,
    resolve_entities,
    sweep_thresholds,
)

_AT = datetime(1970, 1, 1, tzinfo=UTC)

# --------------------------------------------------------------------------- #
# A hand-built extraction: one true edge (high conf) + one false edge (low conf).
# --------------------------------------------------------------------------- #

_CHUNK = Chunk(id="cA", text="Ada leads the Platform team.", source_path="org/a.md")


def _mention(surface_form: str, type_: str) -> MentionSpan:
    start = _CHUNK.text.find(surface_form)
    end = start + len(surface_form) if start >= 0 else -1
    return MentionSpan(
        chunk_id=_CHUNK.id, surface_form=surface_form, start=start, end=end, entity_type=type_
    )


def _resolution() -> Resolution:
    """Resolve ``Ada`` (Person) → person:ada and ``Platform`` (Team) → team:platform."""
    return resolve_entities(
        [_mention("Ada", "Person"), _mention("Platform", "Team")],
        [_CHUNK],
    )


def _triple(rel: str, confidence: float) -> CandidateTriple:
    return CandidateTriple(
        src_mention="Ada",
        rel=rel,
        dst_mention="Platform",
        provenance=_CHUNK.id,
        confidence=confidence,
    )


def _extraction() -> PipelineExtraction:
    """A true ``member_of`` edge at conf 0.9 and a false ``part_of`` edge at conf 0.3."""
    extractions = [
        Extraction(
            chunk_id=_CHUNK.id,
            triples=(_triple("member_of", 0.9), _triple("part_of", 0.3)),
        )
    ]
    return PipelineExtraction(chunks=[_CHUNK], extractions=extractions, resolution=_resolution())


def _gold() -> World:
    """The gold graph: person:ada member_of team:platform (only the true edge)."""
    world = World()
    world.add_node(Node(id="person:ada", type="Person", created_at=_AT, aliases=["Ada"]))
    world.add_node(Node(id="team:platform", type="Team", created_at=_AT, aliases=["Platform"]))
    world.add_edge(
        Edge(
            id="edge:member_of:person:ada:team:platform",
            type="member_of",
            src="person:ada",
            dst="team:platform",
            created_at=_AT,
        )
    )
    return world


# --------------------------------------------------------------------------- #
# Curve shape.
# --------------------------------------------------------------------------- #


def test_sweep_orders_thresholds_and_dedupes() -> None:
    report = sweep_thresholds(_extraction(), _gold(), [0.5, 0.0, 0.5, 0.95])
    assert [p.threshold for p in report.points] == [0.0, 0.5, 0.95]


def test_sweep_finds_the_precision_sweet_spot() -> None:
    report = sweep_thresholds(_extraction(), _gold(), [0.0, 0.5, 0.95])
    by_threshold = {p.threshold: p for p in report.points}

    # threshold 0.0 keeps both edges → the false part_of drags precision to 0.5.
    low = by_threshold[0.0]
    assert low.reconstructed_edge_count == 2
    assert low.edges.precision == pytest.approx(0.5)
    assert low.edges.recall == pytest.approx(1.0)

    # threshold 0.5 drops the false edge (conf 0.3) but keeps the true one (conf 0.9):
    # perfect edge fidelity — the sweet spot.
    mid = by_threshold[0.5]
    assert mid.reconstructed_edge_count == 1
    assert mid.edges.precision == pytest.approx(1.0)
    assert mid.edges.recall == pytest.approx(1.0)
    assert mid.edges.f1 == pytest.approx(1.0)

    # threshold 0.95 drops the true edge too → recall collapses.
    high = by_threshold[0.95]
    assert high.reconstructed_edge_count == 0
    assert high.edges.recall == pytest.approx(0.0)

    # The harness's headline answer is the sweet spot.
    best = report.best_edge_f1()
    assert best is not None
    assert best.threshold == 0.5
    assert best.edges.f1 > low.edges.f1


def test_sweep_recall_and_edge_count_are_monotone_non_increasing() -> None:
    report = sweep_thresholds(_extraction(), _gold(), [0.0, 0.25, 0.5, 0.75, 0.95])
    recalls = [p.edges.recall for p in report.points]
    counts = [p.reconstructed_edge_count for p in report.points]
    assert recalls == sorted(recalls, reverse=True)
    assert counts == sorted(counts, reverse=True)


def test_sweep_node_metrics_are_invariant_across_thresholds() -> None:
    # The threshold gates edges only; every point sees the same two-node graph.
    report = sweep_thresholds(_extraction(), _gold(), [0.0, 0.5, 0.95])
    f1s = {p.nodes.f1 for p in report.points}
    counts = {p.reconstructed_node_count for p in report.points}
    assert f1s == {1.0}
    assert counts == {2}


def test_sweep_reuses_one_extraction_without_rebuilding() -> None:
    # build() re-aggregates the SAME chunks/extractions/resolution objects each time.
    extraction = _extraction()
    report = sweep_thresholds(extraction, _gold(), [0.0, 0.5])
    assert len(report.points) == 2
    # The extraction is untouched — no per-threshold re-extraction mutated it.
    assert extraction.chunks == [_CHUNK]
    assert len(extraction.extractions[0].triples) == 2


# --------------------------------------------------------------------------- #
# Serialization.
# --------------------------------------------------------------------------- #


def test_sweep_report_json_round_trips_shape() -> None:
    report = sweep_thresholds(_extraction(), _gold(), [0.0, 0.5, 0.95])
    data = json.loads(report.to_json())
    assert data["gold_edge_count"] == 1
    assert [p["threshold"] for p in data["points"]] == [0.0, 0.5, 0.95]
    assert data["best_edge_f1_threshold"] == 0.5
    # Every point carries node + edge P/R/F1.
    for point in data["points"]:
        assert set(point["edges"]) >= {"precision", "recall", "f1"}


def test_sweep_report_markdown_has_a_row_per_threshold() -> None:
    report = sweep_thresholds(_extraction(), _gold(), [0.0, 0.5, 0.95])
    md = report.to_markdown()
    assert "# Reconstruct edge-threshold sweep" in md
    assert "threshold" in md and "edge F1" in md
    # header + separator + one row per threshold.
    assert md.count("\n| ") >= 2 + 3
    assert "**Best edge F1:**" in md


# --------------------------------------------------------------------------- #
# Keyless end to end (fake backend).
# --------------------------------------------------------------------------- #


def _golden_run_dir(root: Path) -> str:
    """A full golden run (corpus artifacts + gold ``kg/``) the sweep can point ``--run`` at."""
    from enterprise_sim.benchmark.fixtures import golden_run

    return str(golden_run(root).run_dir)


def test_sweep_keyless_over_golden_run() -> None:
    """The documented keyless path: extract a fresh golden run once, sweep many thresholds."""
    import tempfile

    from enterprise_sim.benchmark.fixtures import golden_run

    with tempfile.TemporaryDirectory(prefix="esim-sweep-test-") as tmp:
        run = golden_run(tmp)
        extraction = extract_once(str(run.run_dir), build_client(LLMConfig(backend="fake")))
        # Fake confidences clamp to [0, 1]; a threshold above 1.0 gates every edge away.
        report = sweep_thresholds(extraction, run.world, [0.0, 0.5, 1.0, 1.1])

    assert [p.threshold for p in report.points] == [0.0, 0.5, 1.0, 1.1]
    assert report.gold_node_count > 0
    # The knob is monotone: recall + kept edges never rise as the threshold climbs.
    counts = [p.reconstructed_edge_count for p in report.points]
    recalls = [p.edges.recall for p in report.points]
    assert counts == sorted(counts, reverse=True)
    assert recalls == sorted(recalls, reverse=True)
    # Above the max clamped confidence, every edge is dropped.
    assert report.points[-1].reconstructed_edge_count == 0


def test_sweep_is_deterministic() -> None:
    a = sweep_thresholds(_extraction(), _gold(), [0.0, 0.5, 0.95])
    b = sweep_thresholds(_extraction(), _gold(), [0.0, 0.5, 0.95])
    assert a.to_json() == b.to_json()


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def test_sweep_subcommand_is_registered_with_default_thresholds() -> None:
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "sweep"])
    assert args.func is not None
    assert args.thresholds == [0.0, 0.25, 0.5, 0.75]
    assert args.backend == "fake"


def test_sweep_cli_parses_thresholds() -> None:
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "sweep", "--thresholds", "0,0.3, 0.6 ,0.9"])
    assert args.thresholds == [0.0, 0.3, 0.6, 0.9]


def test_sweep_cli_rejects_bad_thresholds() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["reconstruct", "sweep", "--thresholds", "not-a-number"])


def test_sweep_cli_writes_markdown_table(tmp_path: Path, capsys: Any) -> None:
    run_dir = _golden_run_dir(tmp_path / "run")
    out = tmp_path / "sweep.md"
    rc = main(
        [
            "reconstruct",
            "sweep",
            "--run",
            run_dir,
            "--thresholds",
            "0,0.5,1.1",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "Reconstruct edge-threshold sweep" in text
    assert "| threshold |" in text
    err = capsys.readouterr().err
    assert "reconstruct sweep" in err
    assert "3 thresholds" in err


def test_sweep_cli_json_to_stdout(tmp_path: Path, capsys: Any) -> None:
    run_dir = _golden_run_dir(tmp_path / "run")
    rc = main(["reconstruct", "sweep", "--run", run_dir, "--thresholds", "0,1.1", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["threshold"] for p in data["points"]] == [0.0, 1.1]
