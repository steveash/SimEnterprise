"""Relation aggregation + build-once persistence tests (esim-nc6.5).

Covers the reconstruct pipeline's terminal stage — candidate triples + resolved
entities → a persisted :class:`~enterprise_sim.reconstruct.schema.ReconstructedKG`
— along the axes the acceptance criteria name:

* **Relation aggregation** — triple surface forms are rewritten over canonical ids,
  deduped into one edge per ``(src, rel, dst)`` carrying a support count and merged
  provenance; unresolved endpoints and self-loops are dropped.
* **Confidence threshold** — the edge confidence knob trades recall for precision.
* **Build + persist** — the assembled KG is loadable (same gold schema), scores
  through the fidelity scorer, and round-trips byte-stably; identical inputs build
  an identical KG (deterministic).
* **Keyless end to end** — ``run_pipeline`` over a fresh golden run and the
  ``reconstruct build`` CLI both emit a small, loadable KG via the fake backend
  with no key.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.llm import LLMConfig, build_client
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.reconstruct import (
    BuildConfig,
    CandidateTriple,
    Chunk,
    Extraction,
    MentionSpan,
    ReconstructedKG,
    Resolution,
    aggregate_relations,
    build_kg,
    resolve_entities,
    run_pipeline,
    score_fidelity,
)

# --------------------------------------------------------------------------- #
# A small hand-built pipeline output: two chunks naming the same two entities.
# --------------------------------------------------------------------------- #

_CHUNK_A = Chunk(id="cA", text="Ada leads the Platform team.", source_path="org/a.md")
_CHUNK_B = Chunk(id="cB", text="Ada is a member of Platform.", source_path="org/b.md")
_CHUNKS = [_CHUNK_A, _CHUNK_B]


def _mention(chunk: Chunk, surface_form: str, type_: str) -> MentionSpan:
    start = chunk.text.find(surface_form)
    end = start + len(surface_form) if start >= 0 else -1
    return MentionSpan(
        chunk_id=chunk.id, surface_form=surface_form, start=start, end=end, entity_type=type_
    )


def _resolution() -> Resolution:
    """Resolve ``Ada`` (Person) and ``Platform`` (Team), each named in both chunks."""
    mentions = [
        _mention(_CHUNK_A, "Ada", "Person"),
        _mention(_CHUNK_A, "Platform", "Team"),
        _mention(_CHUNK_B, "Ada", "Person"),
        _mention(_CHUNK_B, "Platform", "Team"),
    ]
    return resolve_entities(mentions, _CHUNKS)


def _triple(
    src: str, rel: str, dst: str, chunk_id: str, confidence: float = 1.0
) -> CandidateTriple:
    return CandidateTriple(
        src_mention=src, rel=rel, dst_mention=dst, provenance=chunk_id, confidence=confidence
    )


def _extractions(triples_by_chunk: dict[str, Sequence[CandidateTriple]]) -> list[Extraction]:
    return [
        Extraction(chunk_id=cid, triples=tuple(triples_by_chunk.get(cid, ())))
        for cid in ("cA", "cB")
    ]


def test_resolution_ids_are_predictable() -> None:
    ids = {e.type: e.id for e in _resolution().entities}
    assert ids == {"Person": "person:ada", "Team": "team:platform"}


# --------------------------------------------------------------------------- #
# Relation aggregation.
# --------------------------------------------------------------------------- #


def test_aggregate_rewrites_endpoints_and_dedupes_with_support() -> None:
    resolution = _resolution()
    # The same relation is attested by both chunks at different confidences.
    extractions = _extractions(
        {
            "cA": [_triple("Ada", "member_of", "Platform", "cA", confidence=0.9)],
            "cB": [_triple("Ada", "member_of", "Platform", "cB", confidence=0.6)],
        }
    )
    edges, provenance = aggregate_relations(extractions, resolution, _CHUNKS)

    assert len(edges) == 1
    edge = edges[0]
    assert (edge.src, edge.type, edge.dst) == ("person:ada", "member_of", "team:platform")
    # Deduped: one edge, support = 2, confidence = the greatest attesting confidence.
    assert edge.props["support"] == 2
    assert edge.props["confidence"] == 0.9

    # Provenance merges both chunks and both artifacts.
    (prov,) = provenance
    assert prov.target_id == edge.id
    assert prov.chunk_ids == ("cA", "cB")
    assert prov.source_paths == ("org/a.md", "org/b.md")


def test_aggregate_drops_unresolved_endpoints_and_self_loops() -> None:
    resolution = _resolution()
    extractions = _extractions(
        {
            "cA": [
                # dst names no resolved mention → not an edge.
                _triple("Ada", "authored", "Some Missing Doc", "cA"),
                # both endpoints resolve to the same entity → self-loop, dropped.
                _triple("Ada", "collaborates_with", "Ada", "cA"),
            ],
        }
    )
    edges, provenance = aggregate_relations(extractions, resolution, _CHUNKS)
    assert edges == []
    assert provenance == []


def test_edge_confidence_threshold_is_the_precision_knob() -> None:
    resolution = _resolution()
    extractions = _extractions(
        {"cA": [_triple("Ada", "member_of", "Platform", "cA", confidence=0.65)]}
    )
    # Below the bar → dropped; at/above → kept.
    assert aggregate_relations(extractions, resolution, _CHUNKS, threshold=0.7)[0] == []
    kept, _ = aggregate_relations(extractions, resolution, _CHUNKS, threshold=0.6)
    assert len(kept) == 1


def test_aggregate_endpoints_match_across_chunks() -> None:
    """A triple resolves against its own chunk's mention of the surface form."""
    resolution = _resolution()
    extractions = _extractions(
        {
            "cA": [_triple("Ada", "member_of", "Platform", "cA")],
            "cB": [_triple("Platform", "part_of", "Ada", "cB")],  # a different relation
        }
    )
    edges, _ = aggregate_relations(extractions, resolution, _CHUNKS)
    triples = {(e.src, e.type, e.dst) for e in edges}
    assert triples == {
        ("person:ada", "member_of", "team:platform"),
        ("team:platform", "part_of", "person:ada"),
    }


# --------------------------------------------------------------------------- #
# Build + persist.
# --------------------------------------------------------------------------- #


def test_build_kg_assembles_nodes_edges_and_provenance() -> None:
    resolution = _resolution()
    extractions = _extractions({"cA": [_triple("Ada", "member_of", "Platform", "cA")]})
    kg = build_kg(_CHUNKS, extractions, resolution)

    assert {n.id for n in kg.nodes} == {"person:ada", "team:platform"}
    assert {(e.src, e.type, e.dst) for e in kg.edges} == {
        ("person:ada", "member_of", "team:platform")
    }
    # Provenance covers every node and every edge.
    targets = {p.target_id for p in kg.provenance}
    assert targets == {"person:ada", "team:platform", kg.edges[0].id}

    # Node provenance points back to the chunks/artifacts the entity was seen in.
    ada_prov = next(p for p in kg.provenance if p.target_id == "person:ada")
    assert ada_prov.chunk_ids == ("cA", "cB")
    assert ada_prov.source_paths == ("org/a.md", "org/b.md")


def test_built_kg_round_trips_and_scores(tmp_path: Path) -> None:
    resolution = _resolution()
    extractions = _extractions({"cA": [_triple("Ada", "member_of", "Platform", "cA")]})
    kg = build_kg(_CHUNKS, extractions, resolution)

    out = tmp_path / "recon"
    kg.write(out)
    back = ReconstructedKG.read(out)
    assert back.node_count == kg.node_count
    assert back.edge_count == kg.edge_count
    assert {p.target_id for p in back.provenance} == {p.target_id for p in kg.provenance}

    # Scoring the reconstruction against its own graph is perfect (loadable schema).
    report = score_fidelity(kg, kg.to_world())
    assert report.nodes.overall.f1 == 1.0
    assert report.edges.overall.f1 == 1.0


def test_goal_statements_reconstruct_and_score_above_zero() -> None:
    """End to end (keyless): goal statements → Goal nodes + tree edge → F1 > 0.

    Mirrors the recovery path esim-ecr.1 fixes: the extractor emits each objective
    *statement* as a Goal mention (full sentence as surface form) plus the
    ``subgoal_of`` tree edge; resolution + build assemble Goal nodes labeled with the
    statement, which the fidelity scorer aligns to the gold ``goal:N`` ids by
    statement text — recovering goals that previously scored F1 0.000.
    """
    from datetime import datetime

    ts = datetime(1970, 1, 1)
    parent = "Expand into two new regional markets."
    child = "Stand up the supporting platform and tooling."
    chunk = Chunk(
        id="cG",
        text=f"## Goals\n- **{parent}**\n    - {child}\n",
        source_path="org/company.md",
    )

    mentions = [_mention(chunk, parent, "Goal"), _mention(chunk, child, "Goal")]
    assert all(m.start >= 0 for m in mentions)  # both statements located verbatim
    resolution = resolve_entities(mentions, [chunk])
    extractions = [
        Extraction(
            chunk_id="cG",
            mentions=tuple(mentions),
            triples=(_triple(child, "subgoal_of", parent, "cG"),),
        )
    ]
    kg = build_kg([chunk], extractions, resolution)

    # The gold KG's goal shape: statement text as label/alias, ``goal:N`` ids.
    gold = World()
    gold.add_node(
        Node(id="goal:1", type="Goal", created_at=ts, props={"statement": parent}, aliases=[parent])
    )
    gold.add_node(
        Node(id="goal:1.1", type="Goal", created_at=ts, props={"statement": child}, aliases=[child])
    )
    gold.add_edge(
        Edge(
            id="edge:subgoal_of:1.1:1",
            type="subgoal_of",
            src="goal:1.1",
            dst="goal:1",
            created_at=ts,
        )
    )

    report = score_fidelity(kg, gold)
    # Goals are recovered and aligned by statement — the F1 > 0 acceptance bar.
    assert report.nodes.by_type["Goal"].f1 > 0.0
    assert report.nodes.by_type["Goal"].true_positives == 2
    # The goal tree is answerable: the subgoal_of edge round-trips to gold.
    assert report.edges.by_type["subgoal_of"].f1 == 1.0


def test_build_is_deterministic(tmp_path: Path) -> None:
    extractions = _extractions({"cA": [_triple("Ada", "member_of", "Platform", "cA")]})
    a = tmp_path / "a"
    b = tmp_path / "b"
    build_kg(_CHUNKS, extractions, _resolution()).write(a)
    build_kg(_CHUNKS, extractions, _resolution()).write(b)
    for name in ("nodes.jsonl", "edges.jsonl", "provenance.jsonl"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


# --------------------------------------------------------------------------- #
# Mention → provenance grounding aggregation (esim-ecr.2).
# --------------------------------------------------------------------------- #


def test_entity_groundings_aggregate_mentions_into_grounding_artifacts() -> None:
    # No triples: the KG is nodes + node provenance only. Each entity's grounding is
    # the set of artifacts its MentionSpans were carved from, aggregated by node id.
    kg = build_kg(_CHUNKS, _extractions({}), _resolution())
    assert kg.entity_groundings() == {
        "person:ada": ["org/a.md", "org/b.md"],
        "team:platform": ["org/a.md", "org/b.md"],
    }


def test_entity_groundings_exclude_edge_provenance() -> None:
    # An aggregated edge also gets a Provenance record keyed by its edge id; grounding
    # must count only node targets, so edge provenance never leaks in as an entity.
    extractions = _extractions({"cA": [_triple("Ada", "member_of", "Platform", "cA")]})
    kg = build_kg(_CHUNKS, extractions, _resolution())
    edge_ids = {e.id for e in kg.edges}
    assert edge_ids  # an edge was built
    groundings = kg.entity_groundings()
    assert set(groundings) == {"person:ada", "team:platform"}
    assert edge_ids.isdisjoint(groundings)


def test_project_with_groundings_mints_mention_edges_to_grounding_artifacts() -> None:
    from enterprise_sim.benchmark.runners.projection import MENTIONS_EDGE_TYPE, GraphModel
    from enterprise_sim.reconstruct import project_with_groundings

    kg = build_kg(_CHUNKS, _extractions({}), _resolution())
    world, groundings = project_with_groundings(kg)

    # Grounding artifacts absent as reconstructed entities are synthesized as Artifact
    # nodes so each mention edge has a real endpoint.
    artifact_paths = {n.props.get("path") for n in world.nodes() if n.type == "Artifact"}
    assert {"org/a.md", "org/b.md"} <= artifact_paths

    # The projection derives mentions edges (artifact → entity) so provenance
    # questions are answerable over the reconstructed KG.
    model = GraphModel.from_world(world, groundings)
    mention_edges = {(e.src, e.dst) for e in model.edges if e.type == MENTIONS_EDGE_TYPE}
    assert ("org/a.md", "person:ada") in mention_edges
    assert ("org/b.md", "team:platform") in mention_edges


def test_project_with_groundings_is_deterministic() -> None:
    from enterprise_sim.reconstruct import project_with_groundings

    kg = build_kg(_CHUNKS, _extractions({}), _resolution())
    world_a, grounding_a = project_with_groundings(kg)
    world_b, grounding_b = project_with_groundings(kg)
    assert grounding_a == grounding_b
    assert [n.id for n in world_a.nodes()] == [n.id for n in world_b.nodes()]


# --------------------------------------------------------------------------- #
# Keyless end to end (fake backend).
# --------------------------------------------------------------------------- #


def _write_run(root: Path) -> Path:
    """A tiny raw corpus (one markdown artifact) the chunker can read."""
    (root / "org").mkdir(parents=True, exist_ok=True)
    (root / "org" / "team.md").write_text(
        "# Platform Team\n\nAda Lovelace leads the Platform team.\n",
        encoding="utf-8",
    )
    return root


def test_run_pipeline_keyless_emits_loadable_kg(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    kg = run_pipeline(str(run_dir), client, config=BuildConfig())
    # The fake backend yields a small KG — at minimum nodes, all loadable.
    assert kg.node_count >= 1
    out = tmp_path / "recon"
    kg.write(out)
    assert ReconstructedKG.read(out).node_count == kg.node_count


def test_run_pipeline_is_deterministic_over_a_run(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="fake"))
    first = run_pipeline(str(run_dir), client)
    second = run_pipeline(str(run_dir), client)
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert [e.id for e in first.edges] == [e.id for e in second.edges]


def test_run_pipeline_over_golden_run_keyless() -> None:
    """The documented keyless path: a fresh golden run reconstructs via the fake backend."""
    import tempfile

    from enterprise_sim.benchmark.fixtures import golden_run

    with tempfile.TemporaryDirectory(prefix="esim-build-test-") as tmp:
        run = golden_run(tmp)
        kg = run_pipeline(str(run.run_dir), build_client(LLMConfig(backend="fake")))
    assert kg.node_count >= 1
    # Loadable by the fidelity scorer against the gold world.
    report = score_fidelity(kg, run.world)
    assert report.gold_node_count > 0


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def test_build_subcommand_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "build", "-o", "out"])
    assert args.func is not None
    assert args.output == Path("out")
    assert args.backend == "fake"


def test_build_cli_writes_loadable_kg(tmp_path: Path, capsys: Any) -> None:
    run_dir = _write_run(tmp_path / "run")
    out = tmp_path / "recon"
    rc = main(["reconstruct", "build", "--run", str(run_dir), "-o", str(out)])
    assert rc == 0

    kg = ReconstructedKG.read(out)
    assert kg.node_count >= 1
    assert (out / "provenance.jsonl").is_file()
    # A one-line summary goes to stderr.
    err = capsys.readouterr().err
    assert "reconstruct build" in err
    assert f"{kg.node_count} nodes" in err


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="real extraction needs ANTHROPIC_API_KEY; keyless CI covers the fake path",
)
def test_run_pipeline_real_backend(tmp_path: Path) -> None:  # pragma: no cover - needs a key
    run_dir = _write_run(tmp_path / "run")
    client = build_client(LLMConfig(backend="anthropic_api"))
    kg = run_pipeline(str(run_dir), client)
    assert kg.node_count >= 1
