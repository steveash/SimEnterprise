"""Graph-fidelity scorer tests (esim-nc6.6): reconstructed KG vs. gold KG.

Exercises the pure, deterministic scorer against the persistence format alone —
no live pipeline:

* **gold-vs-gold** — node + edge F1 = 1.0, zero entity-resolution errors;
* **gold-vs-perturbed** — dropped / added / merged / split nodes and edges each
  produce the expected precision/recall degradation and over/under-merge counts;
* **name-based alignment** — a reconstruction with different node ids but matching
  type + aliases still aligns and scores 1.0;
* rendering (markdown + JSON) and the ``reconstruct fidelity`` CLI end to end.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.reconstruct import Provenance, ReconstructedKG, score_fidelity
from enterprise_sim.reconstruct.fidelity import PRF

_TS = datetime(2026, 1, 2, 3, 4, 5)


def _node(node_id: str, node_type: str, aliases: list[str], name: str | None = None) -> Node:
    props: dict[str, Any] = {"name": name} if name is not None else {}
    return Node(id=node_id, type=node_type, created_at=_TS, props=props, aliases=aliases)


def _edge(edge_id: str, edge_type: str, src: str, dst: str) -> Edge:
    return Edge(id=edge_id, type=edge_type, src=src, dst=dst, created_at=_TS)


def _gold_world() -> World:
    """A small gold KG: two people, a company, and two relations."""
    world = World()
    world.add_node(_node("person:ada", "Person", ["Ada Lovelace", "Ada"], "Ada Lovelace"))
    world.add_node(
        _node("person:babbage", "Person", ["Charles Babbage", "Babbage"], "Charles Babbage")
    )
    world.add_node(_node("company:acme", "Company", ["Acme Corp", "Acme"], "Acme Corp"))
    world.add_edge(
        _edge("edge:reports_to:ada:babbage", "reports_to", "person:ada", "person:babbage")
    )
    world.add_edge(_edge("edge:works_at:ada:acme", "works_at", "person:ada", "company:acme"))
    return world


def _kg_from_world(world: World) -> ReconstructedKG:
    """A reconstruction that is byte-for-byte the gold graph (same ids)."""
    kg = ReconstructedKG()
    for node in world.nodes():
        kg.add_node(node)
    for edge in world.edges():
        kg.add_edge(edge)
    return kg


# --------------------------------------------------------------------------- #
# The perfect case: gold vs. gold.
# --------------------------------------------------------------------------- #


def test_gold_vs_gold_is_perfect() -> None:
    gold = _gold_world()
    report = score_fidelity(_kg_from_world(gold), gold)

    assert report.nodes.overall.f1 == 1.0
    assert report.nodes.overall.precision == 1.0
    assert report.nodes.overall.recall == 1.0
    assert report.edges.overall.f1 == 1.0
    assert report.entity_resolution.over_merges == 0
    assert report.entity_resolution.under_merges == 0
    assert report.nodes.unmatched_reconstructed == ()
    assert report.nodes.unmatched_gold == ()


def test_gold_vs_gold_per_type_all_perfect() -> None:
    gold = _gold_world()
    report = score_fidelity(_kg_from_world(gold), gold)
    for prf in report.nodes.by_type.values():
        assert prf.f1 == 1.0
    for prf in report.edges.by_type.values():
        assert prf.f1 == 1.0
    assert set(report.nodes.by_type) == {"Person", "Company"}
    assert set(report.edges.by_type) == {"reports_to", "works_at"}


def test_empty_vs_empty_is_perfect() -> None:
    report = score_fidelity(ReconstructedKG(), World())
    assert report.nodes.overall.f1 == 1.0
    assert report.edges.overall.f1 == 1.0
    assert report.entity_resolution.over_merges == 0


# --------------------------------------------------------------------------- #
# Alignment by type + name (ids differ from gold).
# --------------------------------------------------------------------------- #


def test_name_alignment_recovers_perfect_score_with_different_ids() -> None:
    gold = _gold_world()
    kg = ReconstructedKG()
    # Different, content-derived ids; names/aliases match the gold surface forms.
    kg.add_node(_node("n:1", "Person", ["ada lovelace"]))
    kg.add_node(_node("n:2", "Person", ["Babbage"]))
    kg.add_node(_node("n:3", "Company", ["ACME"]))
    kg.add_edge(_edge("e:1", "reports_to", "n:1", "n:2"))
    kg.add_edge(_edge("e:2", "works_at", "n:1", "n:3"))

    report = score_fidelity(kg, gold)
    assert report.nodes.overall.f1 == 1.0
    assert report.edges.overall.f1 == 1.0
    assert report.entity_resolution.over_merges == 0
    assert report.entity_resolution.under_merges == 0
    assert report.nodes.alignment == {
        "n:1": "person:ada",
        "n:2": "person:babbage",
        "n:3": "company:acme",
    }


def test_type_mismatch_blocks_name_alignment() -> None:
    gold = _gold_world()
    kg = ReconstructedKG()
    # Right name, wrong type -> no candidate, stays unmatched.
    kg.add_node(_node("n:1", "Company", ["Ada Lovelace"]))
    report = score_fidelity(kg, gold)
    assert report.nodes.alignment == {}
    assert report.nodes.unmatched_reconstructed == ("n:1",)


# --------------------------------------------------------------------------- #
# Goal alignment: statement-text match, robust to trailing-punctuation drift.
# --------------------------------------------------------------------------- #


def _goal_gold_world() -> World:
    """A gold KG shaped like the builder's goals: statement text as label/alias."""
    world = World()
    world.add_node(_node("goal:1", "Goal", ["Expand into two new regional markets."]))
    world.add_node(_node("goal:1.1", "Goal", ["Stand up the supporting platform and tooling."]))
    world.add_edge(_edge("edge:subgoal_of:1.1:1", "subgoal_of", "goal:1.1", "goal:1"))
    return world


def test_goal_aligns_by_statement_text() -> None:
    # A reconstructed Goal labeled with the gold statement aligns despite a
    # content-derived id different from the gold ``goal:N`` id.
    gold = _goal_gold_world()
    kg = ReconstructedKG()
    kg.add_node(
        _node(
            "goal:expand-into-two-new-regional-markets",
            "Goal",
            [],
            "Expand into two new regional markets.",
        )
    )
    report = score_fidelity(kg, gold)
    assert report.nodes.alignment == {"goal:expand-into-two-new-regional-markets": "goal:1"}
    assert report.nodes.by_type["Goal"].true_positives == 1


def test_goal_aligns_when_trailing_period_dropped() -> None:
    # The extractor copied the statement without its terminal period; the two still
    # align on the trailing-punctuation-trimmed form, so the goal is recovered.
    gold = _goal_gold_world()
    kg = ReconstructedKG()
    kg.add_node(_node("g:1", "Goal", ["Expand into two new regional markets"]))  # no period
    kg.add_node(_node("g:2", "Goal", ["Stand up the supporting platform and tooling"]))
    kg.add_edge(_edge("e:sub", "subgoal_of", "g:2", "g:1"))

    report = score_fidelity(kg, gold)
    assert report.nodes.by_type["Goal"].f1 == 1.0
    # Goal-tree edge is recovered too, since both endpoints aligned.
    assert report.edges.by_type["subgoal_of"].f1 == 1.0


# --------------------------------------------------------------------------- #
# Perturbations: dropped / added nodes and edges.
# --------------------------------------------------------------------------- #


def test_dropped_node_lowers_recall_only() -> None:
    gold = _gold_world()
    kg = _kg_from_world(gold)
    kg.nodes = [n for n in kg.nodes if n.id != "company:acme"]
    kg.edges = [e for e in kg.edges if e.dst != "company:acme"]  # drop its dangling edge too

    report = score_fidelity(kg, gold)
    # 2 of 3 gold nodes recovered; both recovered nodes are correct.
    assert report.nodes.overall == PRF(true_positives=2, predicted=2, gold=3)
    assert report.nodes.overall.precision == 1.0
    assert report.nodes.overall.recall == pytest.approx(2 / 3)
    assert report.nodes.unmatched_gold == ("company:acme",)
    # 1 of 2 gold edges recovered.
    assert report.edges.overall == PRF(true_positives=1, predicted=1, gold=2)


def test_added_node_lowers_precision_only() -> None:
    gold = _gold_world()
    kg = _kg_from_world(gold)
    kg.add_node(_node("person:ghost", "Person", ["Grace Hopper"], "Grace Hopper"))

    report = score_fidelity(kg, gold)
    assert report.nodes.overall == PRF(true_positives=3, predicted=4, gold=3)
    assert report.nodes.overall.recall == 1.0
    assert report.nodes.overall.precision == pytest.approx(3 / 4)
    assert report.nodes.unmatched_reconstructed == ("person:ghost",)
    assert report.nodes.unmatched_gold == ()


def test_added_edge_lowers_edge_precision() -> None:
    gold = _gold_world()
    kg = _kg_from_world(gold)
    kg.add_edge(_edge("edge:reports_to:ada:acme", "reports_to", "person:ada", "company:acme"))
    report = score_fidelity(kg, gold)
    assert report.edges.overall == PRF(true_positives=2, predicted=3, gold=2)
    assert report.edges.overall.recall == 1.0
    assert report.edges.overall.precision == pytest.approx(2 / 3)


def test_wrong_edge_endpoint_is_a_miss() -> None:
    gold = _gold_world()
    kg = _kg_from_world(gold)
    # Repoint reports_to to the wrong destination: gold triple lost, new one wrong.
    kg.edges = [
        _edge("edge:reports_to:ada:babbage", "reports_to", "person:ada", "company:acme")
        if e.id == "edge:reports_to:ada:babbage"
        else e
        for e in kg.edges
    ]
    report = score_fidelity(kg, gold)
    assert report.edges.overall == PRF(true_positives=1, predicted=2, gold=2)


# --------------------------------------------------------------------------- #
# Entity-resolution errors: over-merge and under-merge.
# --------------------------------------------------------------------------- #


def test_over_merge_two_gold_entities_into_one_node() -> None:
    gold = _gold_world()
    kg = ReconstructedKG()
    # One reconstructed node carrying BOTH people's surface forms.
    kg.add_node(_node("n:merged", "Person", ["Ada Lovelace", "Charles Babbage"]))
    kg.add_node(_node("n:acme", "Company", ["Acme"]))

    report = score_fidelity(kg, gold)
    er = report.entity_resolution
    assert er.over_merges == 1
    assert er.under_merges == 0
    (rid, gids) = er.over_merge_detail[0]
    assert rid == "n:merged"
    assert set(gids) == {"person:ada", "person:babbage"}


def test_under_merge_one_gold_entity_split_across_nodes() -> None:
    gold = _gold_world()
    kg = ReconstructedKG()
    # Two reconstructed nodes both claiming to be Ada.
    kg.add_node(_node("n:ada-a", "Person", ["Ada Lovelace"]))
    kg.add_node(_node("n:ada-b", "Person", ["Ada"]))

    report = score_fidelity(kg, gold)
    er = report.entity_resolution
    assert er.under_merges == 1
    assert er.over_merges == 0
    (gid, rids) = er.under_merge_detail[0]
    assert gid == "person:ada"
    assert set(rids) == {"n:ada-a", "n:ada-b"}
    # Only one of the two split nodes wins the 1:1 alignment.
    assert list(report.nodes.alignment.values()).count("person:ada") == 1


def test_exact_id_match_immunizes_gold_vs_gold_from_name_collisions() -> None:
    # Two distinct gold entities sharing a name + type would be an ER ambiguity by
    # name — but id-anchoring aligns each to its twin, so gold-vs-gold stays clean.
    gold = World()
    gold.add_node(_node("person:jsmith-1", "Person", ["John Smith"], "John Smith"))
    gold.add_node(_node("person:jsmith-2", "Person", ["John Smith"], "John Smith"))
    report = score_fidelity(_kg_from_world(gold), gold)
    assert report.nodes.overall.f1 == 1.0
    assert report.entity_resolution.over_merges == 0
    assert report.entity_resolution.under_merges == 0


# --------------------------------------------------------------------------- #
# PRF degenerate cases.
# --------------------------------------------------------------------------- #


def test_prf_degenerate_cases() -> None:
    assert PRF(0, 0, 0).f1 == 1.0  # correct "nothing"
    assert PRF(0, 5, 0).precision == 0.0  # spurious predictions, no gold
    assert PRF(0, 5, 0).recall == 0.0  # degenerate resolves to 0 when the other set is nonempty
    assert PRF(0, 0, 5).recall == 0.0  # nothing recovered
    assert PRF(0, 0, 5).precision == 0.0  # no predictions but gold present
    assert PRF(4, 4, 8).f1 == pytest.approx(2 / 3)


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def test_markdown_render_is_deterministic_and_labeled() -> None:
    gold = _gold_world()
    report = score_fidelity(_kg_from_world(gold), gold)
    md = report.to_markdown()
    assert md == report.to_markdown()
    assert "# Reconstruct fidelity" in md
    assert "## Nodes" in md
    assert "## Edges" in md
    assert "## Entity resolution" in md


def test_json_render_round_trips_counts() -> None:
    gold = _gold_world()
    kg = _kg_from_world(gold)
    kg.add_node(_node("person:ghost", "Person", ["Grace Hopper"]))
    report = score_fidelity(kg, gold)
    data = json.loads(report.to_json())
    assert data["nodes"]["overall"]["gold"] == 3
    assert data["nodes"]["overall"]["predicted"] == 4
    assert data["sizes"]["reconstructed_nodes"] == 4
    assert data["entity_resolution"]["over_merges"] == 0


def test_markdown_lists_unmatched_nodes() -> None:
    gold = _gold_world()
    kg = _kg_from_world(gold)
    kg.nodes = [n for n in kg.nodes if n.id != "company:acme"]
    md = score_fidelity(kg, gold).to_markdown()
    assert "## Unmatched nodes" in md
    assert "company:acme" in md


# --------------------------------------------------------------------------- #
# Provenance (grounding) fidelity (esim-ecr.2).
# --------------------------------------------------------------------------- #

#: Gold grounding key: entity id → the artifact paths that mention/ground it.
_GOLD_GROUNDING = {
    "person:ada": ["a.md", "b.md"],
    "company:acme": ["a.md"],
}


def _grounded_kg(world: World, grounding: dict[str, list[str]]) -> ReconstructedKG:
    """A reconstruction of ``world`` carrying node grounding as Provenance records."""
    kg = _kg_from_world(world)
    for entity_id, paths in grounding.items():
        kg.add_provenance(Provenance(target_id=entity_id, source_paths=tuple(paths)))
    return kg


def test_provenance_not_scored_without_gold_grounding() -> None:
    gold = _gold_world()
    report = score_fidelity(_grounded_kg(gold, _GOLD_GROUNDING), gold)
    assert report.provenance is None
    assert "provenance" not in report.to_dict()
    assert "## Provenance" not in report.to_markdown()


def test_provenance_gold_vs_gold_is_perfect() -> None:
    gold = _gold_world()
    report = score_fidelity(
        _grounded_kg(gold, _GOLD_GROUNDING), gold, gold_grounding=_GOLD_GROUNDING
    )
    assert report.provenance is not None
    # Three grounding pairs: (ada,a), (ada,b), (acme,a).
    assert report.provenance.overall == PRF(true_positives=3, predicted=3, gold=3)
    assert report.provenance.overall.f1 == 1.0
    assert report.provenance.by_type["Person"].f1 == 1.0
    assert report.provenance.by_type["Company"].f1 == 1.0


def test_provenance_missing_grounding_lowers_recall_only() -> None:
    gold = _gold_world()
    # Recover only one of the three grounding pairs.
    kg = _grounded_kg(gold, {"person:ada": ["a.md"]})
    report = score_fidelity(kg, gold, gold_grounding=_GOLD_GROUNDING)
    assert report.provenance is not None
    prov = report.provenance.overall
    assert prov.precision == 1.0
    assert prov.recall == pytest.approx(1 / 3)


def test_provenance_spurious_grounding_lowers_precision_only() -> None:
    gold = _gold_world()
    # All gold pairs recovered, plus one artifact that does not ground Babbage.
    kg = _grounded_kg(gold, {**_GOLD_GROUNDING, "person:babbage": ["c.md"]})
    report = score_fidelity(kg, gold, gold_grounding=_GOLD_GROUNDING)
    assert report.provenance is not None
    prov = report.provenance.overall
    assert prov.recall == 1.0
    assert prov.true_positives == 3
    assert prov.predicted == 4
    assert prov.precision == pytest.approx(3 / 4)


def test_provenance_grounding_aligns_by_name_across_ids() -> None:
    # A reconstruction with a different entity id but a matching name still aligns,
    # so its grounding is credited against the gold key (path is the shared join).
    gold = _gold_world()
    kg = ReconstructedKG()
    kg.add_node(_node("e0", "Person", ["Ada Lovelace", "Ada"], "Ada Lovelace"))
    kg.add_provenance(Provenance(target_id="e0", source_paths=("a.md", "b.md")))
    report = score_fidelity(kg, gold, gold_grounding={"person:ada": ["a.md", "b.md"]})
    assert report.provenance is not None
    assert report.provenance.overall.f1 == 1.0


def test_provenance_renders_in_markdown_and_json() -> None:
    gold = _gold_world()
    report = score_fidelity(
        _grounded_kg(gold, _GOLD_GROUNDING), gold, gold_grounding=_GOLD_GROUNDING
    )
    md = report.to_markdown()
    assert "## Provenance (grounding)" in md
    data = json.loads(report.to_json())
    assert data["provenance"]["overall"]["f1"] == 1.0
    assert data["provenance"]["by_type"]["Person"]["gold"] == 2


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def _write_gold_run(root: Path, world: World) -> Path:
    """Write ``world`` in the gold run layout (``root/kg/*.jsonl``); return root."""
    _kg_from_world(world).write(root / "kg")
    return root


def test_fidelity_subcommand_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["reconstruct", "fidelity", "--reconstructed", "x"])
    assert args.func is not None
    assert args.reconstructed == Path("x")


def test_fidelity_cli_gold_vs_gold_json(tmp_path: Path, capsys: Any) -> None:
    gold = _gold_world()
    recon_dir = tmp_path / "recon"
    _kg_from_world(gold).write(recon_dir)
    run_dir = _write_gold_run(tmp_path / "gold", gold)

    rc = main(
        [
            "reconstruct",
            "fidelity",
            "--reconstructed",
            str(recon_dir),
            "--run",
            str(run_dir),
            "--json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["nodes"]["overall"]["f1"] == 1.0
    assert data["edges"]["overall"]["f1"] == 1.0
    assert data["entity_resolution"]["over_merges"] == 0


def test_fidelity_cli_writes_markdown_to_output(tmp_path: Path, capsys: Any) -> None:
    gold = _gold_world()
    recon_dir = tmp_path / "recon"
    _kg_from_world(gold).write(recon_dir)
    run_dir = _write_gold_run(tmp_path / "gold", gold)
    out = tmp_path / "report.md"

    rc = main(
        [
            "reconstruct",
            "fidelity",
            "--reconstructed",
            str(recon_dir),
            "--run",
            str(run_dir),
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    assert "# Reconstruct fidelity" in out.read_text(encoding="utf-8")
    # A one-line summary goes to stderr.
    assert "node F1=1.000" in capsys.readouterr().err


def test_fidelity_cli_scores_provenance_from_mentions(tmp_path: Path, capsys: Any) -> None:
    # A gold run whose mentions.jsonl grounds an entity in an artifact drives the
    # provenance section; a reconstruction that recovers that grounding scores 1.0.
    gold = _gold_world()
    gold.add_node(
        Node(
            id="artifact:doc",
            type="Artifact",
            created_at=_TS,
            props={"path": "doc.md"},
            aliases=["Doc"],
        )
    )
    run_dir = _write_gold_run(tmp_path / "gold", gold)
    (run_dir / "kg" / "mentions.jsonl").write_text(
        json.dumps(
            {
                "artifact_path": "doc.md",
                "entity_id": "person:ada",
                "surface_form": "Ada",
                "locator": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recon_dir = tmp_path / "recon"
    _grounded_kg(gold, {"person:ada": ["doc.md"]}).write(recon_dir)

    rc = main(
        [
            "reconstruct",
            "fidelity",
            "--reconstructed",
            str(recon_dir),
            "--run",
            str(run_dir),
            "--json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["provenance"]["overall"]["f1"] == 1.0
    assert data["provenance"]["overall"]["gold"] == 1


# --------------------------------------------------------------------------- #
# Answer-scorer id alignment (esim-e9z): reconstructed answer id -> gold id.
# --------------------------------------------------------------------------- #


def _gold_with_artifact() -> World:
    """The gold world plus an Artifact node keyed by canonical id, path in props."""
    world = _gold_world()
    world.add_node(
        Node(
            id="artifact:doc:groom",
            type="Artifact",
            created_at=_TS,
            props={"path": "artifacts/init/groom.md"},
            aliases=["Backlog"],
        )
    )
    return world


def test_align_reconstructed_ids_resolves_artifact_path_to_canonical_id() -> None:
    from enterprise_sim.reconstruct.fidelity import align_reconstructed_ids

    gold = _gold_with_artifact()
    alignment = align_reconstructed_ids(_kg_from_world(gold), gold)
    # The gold artifact's PATH maps to its canonical id — the namespace bridge the
    # reconstruction's path-named provenance answers need.
    assert alignment["artifacts/init/groom.md"] == "artifact:doc:groom"


def test_align_reconstructed_ids_maps_renamed_node_to_gold_twin() -> None:
    from enterprise_sim.reconstruct.fidelity import align_reconstructed_ids

    gold = _gold_world()
    # A reconstruction that found Ada under a different id but the same name.
    kg = ReconstructedKG()
    kg.add_node(_node("ent:0001", "Person", ["Ada Lovelace", "Ada"], "Ada Lovelace"))
    alignment = align_reconstructed_ids(kg, gold)
    assert alignment["ent:0001"] == "person:ada"


def test_align_reconstructed_ids_unions_node_and_path_sources() -> None:
    from enterprise_sim.reconstruct.fidelity import align_reconstructed_ids

    gold = _gold_with_artifact()
    kg = ReconstructedKG()
    kg.add_node(_node("ent:0001", "Person", ["Ada Lovelace"], "Ada Lovelace"))
    alignment = align_reconstructed_ids(kg, gold)
    assert alignment["ent:0001"] == "person:ada"  # node alignment
    assert alignment["artifacts/init/groom.md"] == "artifact:doc:groom"  # path resolution


def test_align_reconstructed_ids_is_deterministic() -> None:
    from enterprise_sim.reconstruct.fidelity import align_reconstructed_ids

    gold = _gold_with_artifact()
    kg = _kg_from_world(gold)
    assert align_reconstructed_ids(kg, gold) == align_reconstructed_ids(kg, gold)


def test_align_reconstructed_ids_credits_answer_end_to_end() -> None:
    # The aligner feeds the answer scorer: a path-named artifact answer that scores
    # 0 raw is credited once aligned — the esim-e9z acceptance path, keyless.
    from enterprise_sim.benchmark import Benchmark, QAPair
    from enterprise_sim.benchmark.score import Predictions, score
    from enterprise_sim.reconstruct.fidelity import align_reconstructed_ids

    gold = _gold_with_artifact()
    bench = Benchmark.of(
        [
            QAPair(
                id="q",
                question="which artifacts ground the backlog?",
                qtype="which",
                reasoning_type="provenance",
                expected_ids=("artifact:doc:groom",),
            )
        ]
    )
    preds = Predictions.from_mapping({"q": ["artifacts/init/groom.md"]})
    alignment = align_reconstructed_ids(_kg_from_world(gold), gold)

    assert score(bench, preds).overall.macro_f1 == 0.0
    assert score(bench, preds, alignment=alignment).overall.macro_f1 == 1.0
