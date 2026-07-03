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
from enterprise_sim.reconstruct import ReconstructedKG, score_fidelity
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
