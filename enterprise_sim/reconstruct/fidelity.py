"""Graph-fidelity scorer: reconstructed KG vs. gold KG (esim-nc6.6).

The reconstruct pipeline reads the corpus back out into a
:class:`~enterprise_sim.reconstruct.schema.ReconstructedKG` (epic esim-nc6). This
module measures how faithfully that reconstruction recovers the gold knowledge
graph — **purely and deterministically, with no LLM**. Given a reconstruction and
the gold :class:`~enterprise_sim.core.world.World`, it produces a
:class:`FidelityReport` with three families of metric:

* **Node alignment + P/R/F1.** Reconstructed node ids are aligned to gold node ids
  by *id* (exact match, the common case for a faithful reconstruction) and then by
  *type + canonical/alias name*. Precision/recall/F1 are computed over the aligned
  1:1 matching, overall and per node type; unmatched nodes are reported both ways.
* **Edge P/R/F1.** Every reconstructed edge is rewritten from ``(src, rel, dst)``
  to ``(align(src), rel, align(dst))`` using the node alignment, then the resulting
  triple set is scored against the gold triple set, overall and per relation type.
* **Entity-resolution accuracy.** Two failure modes are counted from the
  name-candidate graph: **over-merges** (one reconstructed node collapses ≥2
  distinct gold entities) and **under-merges** (one gold entity is split across ≥2
  reconstructed nodes).

By construction, scoring a gold graph against itself yields node + edge F1 = 1.0
and zero ER errors (every node id-matches its twin), so the scorer is testable
against the persistence format alone — gold-vs-gold for the perfect case and
gold-vs-perturbed for known degradations — with no live pipeline. Wired to
``enterprise-sim reconstruct fidelity``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise_sim.core.world import Node, World
from enterprise_sim.reconstruct.schema import ReconstructedKG

__all__ = [
    "EdgeFidelity",
    "EntityResolution",
    "FidelityReport",
    "NodeFidelity",
    "PRF",
    "score_fidelity",
]


# --------------------------------------------------------------------------- #
# Precision / recall / F1 over sets (micro-averaged: true positives vs. totals).
# --------------------------------------------------------------------------- #


def _prf(true_positives: int, predicted: int, gold: int) -> tuple[float, float, float]:
    """Precision, recall, F1 from raw counts.

    Precision is undefined with no prediction and recall is undefined with no gold
    element; both degenerate cases resolve to ``1.0`` only when the *other* count
    is also zero (a correct "nothing" answer) and ``0.0`` otherwise. F1 is the
    harmonic mean, or ``0.0`` when precision and recall are both zero. Mirrors the
    benchmark grader's ``_prf`` (esim-uzc.3) so both scorers agree on edge cases.
    """
    precision = true_positives / predicted if predicted else (1.0 if gold == 0 else 0.0)
    recall = true_positives / gold if gold else (1.0 if predicted == 0 else 0.0)
    denom = precision + recall
    f1 = 2.0 * precision * recall / denom if denom else 0.0
    return precision, recall, f1


@dataclass(frozen=True)
class PRF:
    """Precision / recall / F1 for one group, carrying the raw counts.

    Attributes:
        true_positives: Reconstructed elements correctly matched to gold.
        predicted: Total reconstructed elements (precision denominator).
        gold: Total gold elements (recall denominator).
    """

    true_positives: int
    predicted: int
    gold: int

    @property
    def precision(self) -> float:
        """True positives over predicted (see :func:`_prf` for degenerate cases)."""
        return _prf(self.true_positives, self.predicted, self.gold)[0]

    @property
    def recall(self) -> float:
        """True positives over gold (see :func:`_prf` for degenerate cases)."""
        return _prf(self.true_positives, self.predicted, self.gold)[1]

    @property
    def f1(self) -> float:
        """Harmonic mean of :attr:`precision` and :attr:`recall`."""
        return _prf(self.true_positives, self.predicted, self.gold)[2]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (counts plus derived P/R/F1)."""
        return {
            "true_positives": self.true_positives,
            "predicted": self.predicted,
            "gold": self.gold,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


# --------------------------------------------------------------------------- #
# Node-name normalization + alignment.
# --------------------------------------------------------------------------- #


#: Sentence-final punctuation trimmed from a name's trailing edge before matching,
#: so a Goal statement quoted with its period ("… markets.") still aligns with the
#: same statement quoted without one — the common drift when the extractor copies a
#: goal sentence. Trimming only the trailing edge leaves name-shaped types (which
#: rarely end in punctuation) untouched.
_TRAILING_PUNCT = ".!?;:,"


def _normalize(text: str) -> str:
    """Casefold and collapse whitespace so surface forms compare canonically."""
    return " ".join(text.split()).casefold()


def _name_tokens(node: Node) -> frozenset[str]:
    """Normalized surface forms for ``node`` (canonical name + aliases).

    Draws from the node's ``aliases`` (which carry the canonical name plus known
    surface forms) and its ``props["name"]`` when present. Each form contributes its
    normalized value *and* a trailing-punctuation-trimmed variant, so a goal
    statement aligns whether or not its terminal period survived extraction. Empty
    strings are dropped; an entity with no usable name yields an empty set and can
    only be aligned by exact id.
    """
    tokens: set[str] = set()
    forms = [*node.aliases, node.props.get("name")]
    for form in forms:
        if not isinstance(form, str) or not form.strip():
            continue
        normalized = _normalize(form)
        tokens.add(normalized)
        trimmed = normalized.rstrip(_TRAILING_PUNCT).strip()
        if trimmed:
            tokens.add(trimmed)
    return frozenset(tokens)


@dataclass(frozen=True)
class NodeFidelity:
    """Node-level fidelity: the 1:1 alignment plus P/R/F1, overall and per type.

    Attributes:
        overall: Micro-averaged P/R/F1 over all aligned nodes.
        by_type: P/R/F1 per node type (keyed by the *gold* type for matches, the
            reconstructed type for unmatched reconstructed nodes).
        alignment: The chosen 1:1 map from reconstructed node id → gold node id.
        unmatched_reconstructed: Reconstructed node ids with no gold match (sorted).
        unmatched_gold: Gold node ids with no reconstructed match (sorted).
    """

    overall: PRF
    by_type: dict[str, PRF]
    alignment: dict[str, str]
    unmatched_reconstructed: tuple[str, ...]
    unmatched_gold: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "overall": self.overall.to_dict(),
            "by_type": {t: prf.to_dict() for t, prf in sorted(self.by_type.items())},
            "alignment": dict(sorted(self.alignment.items())),
            "unmatched_reconstructed": list(self.unmatched_reconstructed),
            "unmatched_gold": list(self.unmatched_gold),
        }


@dataclass(frozen=True)
class EdgeFidelity:
    """Edge-level fidelity: P/R/F1 over id-aligned ``(src, rel, dst)`` triples.

    Attributes:
        overall: Micro-averaged P/R/F1 over all triples.
        by_type: P/R/F1 per relation type.
    """

    overall: PRF
    by_type: dict[str, PRF]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "overall": self.overall.to_dict(),
            "by_type": {t: prf.to_dict() for t, prf in sorted(self.by_type.items())},
        }


@dataclass(frozen=True)
class EntityResolution:
    """Entity-resolution error counts derived from the name-candidate graph.

    Attributes:
        over_merges: Reconstructed nodes each collapsing ≥2 distinct gold entities.
        under_merges: Gold entities each split across ≥2 reconstructed nodes.
        over_merge_detail: ``(reconstructed_id, (gold_id, ...))`` for each
            over-merge (sorted), naming the gold entities that were collapsed.
        under_merge_detail: ``(gold_id, (reconstructed_id, ...))`` for each
            under-merge (sorted), naming the reconstructed nodes it was split into.
    """

    over_merges: int
    under_merges: int
    over_merge_detail: tuple[tuple[str, tuple[str, ...]], ...] = ()
    under_merge_detail: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "over_merges": self.over_merges,
            "under_merges": self.under_merges,
            "over_merge_detail": [
                {"reconstructed_id": rid, "gold_ids": list(gids)}
                for rid, gids in self.over_merge_detail
            ],
            "under_merge_detail": [
                {"gold_id": gid, "reconstructed_ids": list(rids)}
                for gid, rids in self.under_merge_detail
            ],
        }


@dataclass(frozen=True)
class FidelityReport:
    """The full graph-fidelity result: node, edge, and ER metrics together."""

    nodes: NodeFidelity
    edges: EdgeFidelity
    entity_resolution: EntityResolution
    #: (reconstructed node count, gold node count) — headline sizes for the report.
    reconstructed_node_count: int = 0
    gold_node_count: int = 0
    reconstructed_edge_count: int = 0
    gold_edge_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the whole report."""
        return {
            "sizes": {
                "reconstructed_nodes": self.reconstructed_node_count,
                "gold_nodes": self.gold_node_count,
                "reconstructed_edges": self.reconstructed_edge_count,
                "gold_edges": self.gold_edge_count,
            },
            "nodes": self.nodes.to_dict(),
            "edges": self.edges.to_dict(),
            "entity_resolution": self.entity_resolution.to_dict(),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the report to a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Render the report as a GitHub-flavored markdown document."""
        return render_markdown(self)


# --------------------------------------------------------------------------- #
# Scoring.
# --------------------------------------------------------------------------- #


def _align_nodes(
    reconstructed: Sequence[Node],
    gold: Sequence[Node],
) -> tuple[dict[str, str], EntityResolution]:
    """Align reconstructed → gold node ids and count entity-resolution errors.

    Two-phase, fully deterministic:

    1. **Exact id match.** A reconstructed node whose ``(id, type)`` equals a gold
       node's is aligned to it directly. This is the faithful-reconstruction path
       and makes gold-vs-gold trivially perfect regardless of name collisions.
    2. **Name candidacy.** Each *remaining* reconstructed node is matched to gold
       nodes of the same type that share a normalized name token. Those candidate
       links define the ER errors — a reconstructed node with ≥2 gold candidates is
       an over-merge; a gold node with ≥2 reconstructed candidates is an
       under-merge — and are then resolved into a 1:1 alignment greedily (each
       reconstructed node, in id order, takes its lowest-id free gold candidate).

    Returns the ``reconstructed_id → gold_id`` alignment and the
    :class:`EntityResolution` counts.
    """
    gold_by_id = {n.id: n for n in gold}
    alignment: dict[str, str] = {}
    matched_gold: set[str] = set()
    unresolved: list[Node] = []

    # Phase 1: exact id (+ type) match.
    for node in reconstructed:
        twin = gold_by_id.get(node.id)
        if twin is not None and twin.type == node.type and twin.id not in matched_gold:
            alignment[node.id] = twin.id
            matched_gold.add(twin.id)
        else:
            unresolved.append(node)

    # Phase 2: build the name-candidate graph over what phase 1 left unmatched.
    gold_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for gnode in gold:
        if gnode.id in matched_gold:
            continue
        for token in _name_tokens(gnode):
            gold_index[(gnode.type, token)].append(gnode.id)

    recon_to_golds: dict[str, list[str]] = {}
    gold_to_recons: dict[str, set[str]] = defaultdict(set)
    for node in unresolved:
        candidates: set[str] = set()
        for token in _name_tokens(node):
            candidates.update(gold_index.get((node.type, token), ()))
        ordered = sorted(candidates)
        recon_to_golds[node.id] = ordered
        for gid in ordered:
            gold_to_recons[gid].add(node.id)

    # ER errors are read off the candidate graph (before greedy 1:1 resolution).
    over_detail = tuple(
        (rid, tuple(golds)) for rid, golds in sorted(recon_to_golds.items()) if len(golds) >= 2
    )
    under_detail = tuple(
        (gid, tuple(sorted(recons)))
        for gid, recons in sorted(gold_to_recons.items())
        if len(recons) >= 2
    )
    entity_resolution = EntityResolution(
        over_merges=len(over_detail),
        under_merges=len(under_detail),
        over_merge_detail=over_detail,
        under_merge_detail=under_detail,
    )

    # Resolve candidates into a 1:1 alignment, deterministically.
    for node in sorted(unresolved, key=lambda n: n.id):
        for gid in recon_to_golds.get(node.id, ()):
            if gid not in matched_gold:
                alignment[node.id] = gid
                matched_gold.add(gid)
                break

    return alignment, entity_resolution


def _node_fidelity(
    reconstructed: Sequence[Node],
    gold: Sequence[Node],
    alignment: Mapping[str, str],
) -> NodeFidelity:
    """P/R/F1 over the node alignment, overall and per type, with unmatched lists."""
    gold_type = {n.id: n.type for n in gold}
    matched_gold = set(alignment.values())

    # Per-type tallies keyed by gold type (matches) or reconstructed type (misses).
    tp_by_type: dict[str, int] = defaultdict(int)
    pred_by_type: dict[str, int] = defaultdict(int)
    gold_by_type: dict[str, int] = defaultdict(int)

    for node in reconstructed:
        gid = alignment.get(node.id)
        # A matched node counts under its gold type so a mistyped match is visible.
        pred_by_type[gold_type[gid] if gid is not None else node.type] += 1
        if gid is not None:
            tp_by_type[gold_type[gid]] += 1
    for node in gold:
        gold_by_type[node.type] += 1

    by_type: dict[str, PRF] = {}
    for node_type in set(pred_by_type) | set(gold_by_type):
        by_type[node_type] = PRF(
            true_positives=tp_by_type.get(node_type, 0),
            predicted=pred_by_type.get(node_type, 0),
            gold=gold_by_type.get(node_type, 0),
        )

    overall = PRF(
        true_positives=len(alignment),
        predicted=len(reconstructed),
        gold=len(gold),
    )
    unmatched_recon = tuple(sorted(n.id for n in reconstructed if n.id not in alignment))
    unmatched_gold = tuple(sorted(n.id for n in gold if n.id not in matched_gold))
    return NodeFidelity(
        overall=overall,
        by_type=by_type,
        alignment=dict(alignment),
        unmatched_reconstructed=unmatched_recon,
        unmatched_gold=unmatched_gold,
    )


def _edge_fidelity(
    reconstructed: World,
    gold: World,
    alignment: Mapping[str, str],
) -> EdgeFidelity:
    """P/R/F1 over ``(src, rel, dst)`` triples after aligning reconstructed ids.

    Reconstructed endpoints are mapped through ``alignment`` (falling back to the
    reconstructed id itself when unaligned, so an unmatched endpoint can never
    coincide with a gold id) and the resulting triple *set* is scored against the
    gold triple set — overall and per relation type. Sets deduplicate parallel
    edges of the same ``(src, rel, dst)``.
    """

    def gold_triple(src: str, rel: str, dst: str) -> tuple[str, str, str]:
        return (src, rel, dst)

    gold_triples: set[tuple[str, str, str]] = set()
    gold_by_type: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for edge in gold.edges():
        triple = gold_triple(edge.src, edge.type, edge.dst)
        gold_triples.add(triple)
        gold_by_type[edge.type].add(triple)

    recon_triples: set[tuple[str, str, str]] = set()
    recon_by_type: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for edge in reconstructed.edges():
        triple = gold_triple(
            alignment.get(edge.src, edge.src),
            edge.type,
            alignment.get(edge.dst, edge.dst),
        )
        recon_triples.add(triple)
        recon_by_type[edge.type].add(triple)

    overall = PRF(
        true_positives=len(gold_triples & recon_triples),
        predicted=len(recon_triples),
        gold=len(gold_triples),
    )
    by_type: dict[str, PRF] = {}
    for rel in set(recon_by_type) | set(gold_by_type):
        rec = recon_by_type.get(rel, set())
        gld = gold_by_type.get(rel, set())
        by_type[rel] = PRF(
            true_positives=len(rec & gld),
            predicted=len(rec),
            gold=len(gld),
        )
    return EdgeFidelity(overall=overall, by_type=by_type)


def score_fidelity(reconstructed: ReconstructedKG, gold: World) -> FidelityReport:
    """Score a :class:`ReconstructedKG` against the gold :class:`World`.

    Pure and deterministic (no LLM): aligns nodes by id then name, scores node and
    edge P/R/F1 over that alignment, and counts entity-resolution errors. Scoring
    the gold graph against itself yields node + edge F1 = 1.0 and zero ER errors.
    """
    recon_world = reconstructed.to_world()
    recon_nodes = recon_world.nodes()
    gold_nodes = gold.nodes()

    alignment, entity_resolution = _align_nodes(recon_nodes, gold_nodes)
    nodes = _node_fidelity(recon_nodes, gold_nodes, alignment)
    edges = _edge_fidelity(recon_world, gold, alignment)

    return FidelityReport(
        nodes=nodes,
        edges=edges,
        entity_resolution=entity_resolution,
        reconstructed_node_count=len(recon_nodes),
        gold_node_count=len(gold_nodes),
        reconstructed_edge_count=recon_world.edge_count,
        gold_edge_count=gold.edge_count,
    )


# --------------------------------------------------------------------------- #
# Markdown rendering (CLI).
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    """Render a metric to three decimal places (matching the bench reports)."""
    return f"{value:.3f}"


def _markdown_table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    """A GitHub-flavored markdown table (header + separator + body rows)."""
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _prf_rows(overall: PRF, by_type: Mapping[str, PRF]) -> list[list[str]]:
    """One "overall" row plus a sorted per-type row for a P/R/F1 table."""

    def row(label: str, prf: PRF) -> list[str]:
        return [
            label,
            _fmt(prf.f1),
            _fmt(prf.precision),
            _fmt(prf.recall),
            str(prf.true_positives),
            str(prf.predicted),
            str(prf.gold),
        ]

    rows = [row("**overall**", overall)]
    rows.extend(row(name, by_type[name]) for name in sorted(by_type))
    return rows


def render_markdown(report: FidelityReport, *, title: str = "Reconstruct fidelity") -> str:
    """Render a :class:`FidelityReport` as a markdown document.

    Node and edge P/R/F1 tables (overall + per type), an entity-resolution summary,
    and — when present — the unmatched node ids. Pure: the same report always
    renders the same text.
    """
    header = ["type", "F1", "P", "R", "TP", "pred", "gold"]
    lines = [
        f"# {title}",
        "",
        (
            f"Nodes: {report.reconstructed_node_count} reconstructed vs. "
            f"{report.gold_node_count} gold · "
            f"Edges: {report.reconstructed_edge_count} reconstructed vs. "
            f"{report.gold_edge_count} gold."
        ),
        "",
        "## Nodes",
        "",
    ]
    lines.extend(_markdown_table(header, _prf_rows(report.nodes.overall, report.nodes.by_type)))

    lines.extend(["", "## Edges", ""])
    lines.extend(_markdown_table(header, _prf_rows(report.edges.overall, report.edges.by_type)))

    er = report.entity_resolution
    lines.extend(
        [
            "",
            "## Entity resolution",
            "",
            f"- **Over-merges** (≥2 gold entities collapsed into one node): {er.over_merges}",
            f"- **Under-merges** (one gold entity split across ≥2 nodes): {er.under_merges}",
        ]
    )

    unmatched_recon = report.nodes.unmatched_reconstructed
    unmatched_gold = report.nodes.unmatched_gold
    if unmatched_recon or unmatched_gold:
        lines.extend(["", "## Unmatched nodes", ""])
        if unmatched_gold:
            lines.append(
                f"- Gold with no reconstruction ({len(unmatched_gold)}): "
                f"{', '.join(unmatched_gold)}"
            )
        if unmatched_recon:
            lines.append(
                f"- Reconstructed with no gold ({len(unmatched_recon)}): "
                f"{', '.join(unmatched_recon)}"
            )

    return "\n".join(lines) + "\n"
