"""Reconstruct scaffold tests (esim-nc6.1): schema round-trips, gold-format IO, CLI group.

Covers the foundation the reconstruct pipeline builds on:

* :class:`Chunk`, :class:`MentionSpan`, :class:`CandidateTriple`, :class:`Provenance`
  round-trip to/from a dict;
* :class:`ReconstructedKG` writes ``nodes.jsonl`` / ``edges.jsonl`` in the exact
  gold KG schema — loadable by
  :func:`enterprise_sim.benchmark.generate.load_world_from_run` and projectable by
  :meth:`~enterprise_sim.benchmark.runners.projection.GraphModel.from_world` —
  plus a ``provenance.jsonl`` sidecar, and round-trips byte-stably; and
* ``enterprise-sim reconstruct --help`` lists the command group.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.generate import load_world_from_run
from enterprise_sim.benchmark.runners.projection import GraphModel
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.world import Edge, Node
from enterprise_sim.reconstruct import (
    CandidateTriple,
    Chunk,
    MentionSpan,
    Provenance,
    ReconstructedKG,
)

_TS = datetime(2026, 1, 2, 3, 4, 5)


def _sample_kg() -> ReconstructedKG:
    kg = ReconstructedKG()
    kg.add_node(
        Node(
            id="person:ada-lovelace",
            type="Person",
            created_at=_TS,
            props={"name": "Ada Lovelace"},
            aliases=["Ada Lovelace", "Ada"],
        )
    )
    kg.add_node(
        Node(id="person:charles-babbage", type="Person", created_at=_TS, props={"name": "Babbage"})
    )
    kg.add_edge(
        Edge(
            id="edge:reports_to:ada:babbage",
            type="reports_to",
            src="person:ada-lovelace",
            dst="person:charles-babbage",
            created_at=_TS,
        )
    )
    kg.add_provenance(
        Provenance(
            target_id="edge:reports_to:ada:babbage",
            chunk_ids=("chunk:org-1",),
            source_paths=("emails/org-chart.md",),
        )
    )
    return kg


# -- intermediate schema round-trips ----------------------------------------


def test_chunk_round_trips() -> None:
    chunk = Chunk(
        id="chunk:1",
        text="Ada reports to Babbage.",
        source_path="emails/org.md",
        offset=42,
        length=23,
        section="Org > Leadership",
    )
    assert Chunk.from_dict(chunk.to_dict()) == chunk


def test_chunk_span_length_defaults_to_text_length() -> None:
    assert Chunk(id="c", text="hello", source_path="p").span_length == 5
    assert Chunk(id="c", text="hello", source_path="p", length=3).span_length == 3


def test_mention_span_round_trips() -> None:
    mention = MentionSpan(
        chunk_id="chunk:1",
        surface_form="Ada",
        start=0,
        end=3,
        entity_id="person:ada-lovelace",
    )
    assert MentionSpan.from_dict(mention.to_dict()) == mention


def test_mention_span_unlinked_by_default() -> None:
    mention = MentionSpan(chunk_id="c", surface_form="Ada", start=0, end=3)
    assert mention.entity_id is None
    assert MentionSpan.from_dict(mention.to_dict()) == mention


def test_candidate_triple_round_trips() -> None:
    triple = CandidateTriple(
        src_mention="Ada",
        rel="reports_to",
        dst_mention="Babbage",
        provenance="chunk:1",
        confidence=0.75,
    )
    assert CandidateTriple.from_dict(triple.to_dict()) == triple


def test_candidate_triple_defaults_confidence() -> None:
    triple = CandidateTriple(src_mention="A", rel="r", dst_mention="B", provenance="c")
    assert triple.confidence == 1.0


def test_provenance_round_trips() -> None:
    prov = Provenance(
        target_id="edge:1",
        chunk_ids=("chunk:1", "chunk:2"),
        source_paths=("a.md",),
    )
    restored = Provenance.from_dict(prov.to_dict())
    assert restored == prov
    assert isinstance(restored.chunk_ids, tuple)


# -- ReconstructedKG on-disk format -----------------------------------------


def test_reconstructed_kg_round_trips_through_disk(tmp_path: Path) -> None:
    kg = _sample_kg()
    written = kg.write(tmp_path / "kg")
    assert {p.name for p in written} == {"nodes.jsonl", "edges.jsonl", "provenance.jsonl"}

    loaded = ReconstructedKG.read(tmp_path / "kg")
    assert loaded.nodes == kg.nodes
    assert loaded.edges == kg.edges
    assert loaded.provenance == kg.provenance


def test_write_is_deterministic(tmp_path: Path) -> None:
    _sample_kg().write(tmp_path / "a")
    _sample_kg().write(tmp_path / "b")
    for name in ("nodes.jsonl", "edges.jsonl", "provenance.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_nodes_edges_match_gold_kg_schema(tmp_path: Path) -> None:
    """The written node/edge rows carry exactly the gold KG's keys."""
    import json

    _sample_kg().write(tmp_path / "kg")
    node_row = json.loads((tmp_path / "kg" / "nodes.jsonl").read_text().splitlines()[0])
    edge_row = json.loads((tmp_path / "kg" / "edges.jsonl").read_text().splitlines()[0])
    assert set(node_row) == {"id", "type", "created_at", "props", "aliases"}
    assert set(edge_row) == {"id", "type", "src", "dst", "created_at", "props"}


def test_reconstruction_loads_via_benchmark_world_loader(tmp_path: Path) -> None:
    """A reconstruction is loadable by the benchmark's gold-run world loader."""
    _sample_kg().write(tmp_path / "kg")
    world = load_world_from_run(tmp_path)
    assert world.node_count == 2
    assert world.edge_count == 1
    assert world.get_node("person:ada-lovelace") is not None


def test_reconstruction_projects_via_graph_model() -> None:
    """The graph engines' projection accepts a reconstruction's in-memory world."""
    model = GraphModel.from_world(_sample_kg().to_world())
    assert {n.id for n in model.nodes} == {"person:ada-lovelace", "person:charles-babbage"}
    assert "reports_to" in model.edge_types


def test_read_tolerates_missing_provenance_file(tmp_path: Path) -> None:
    kg = _sample_kg()
    kg.write(tmp_path / "kg")
    (tmp_path / "kg" / "provenance.jsonl").unlink()
    loaded = ReconstructedKG.read(tmp_path / "kg")
    assert loaded.node_count == 2
    assert loaded.provenance == []


def test_empty_reconstruction_round_trips(tmp_path: Path) -> None:
    ReconstructedKG().write(tmp_path / "kg")
    loaded = ReconstructedKG.read(tmp_path / "kg")
    assert loaded.node_count == 0
    assert loaded.edge_count == 0
    assert loaded.provenance == []


# -- CLI --------------------------------------------------------------------


def test_reconstruct_group_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["reconstruct"])
    assert args.command == "reconstruct"
    assert args.func is not None


def test_reconstruct_help_lists_the_group(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["reconstruct", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "reconstruct" in out


def test_reconstruct_without_subcommand_prints_help_and_returns_2(capsys: Any) -> None:
    assert main(["reconstruct"]) == 2
    out = capsys.readouterr().out
    assert "build" in out
