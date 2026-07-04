"""Relation aggregation + build-once KG persistence (esim-nc6.5).

The reconstruct pipeline's terminal stage. Its three predecessors each leave
per-chunk, surface-form-level artifacts:

* chunking (esim-nc6.2) carves the corpus into
  :class:`~enterprise_sim.reconstruct.schema.Chunk`\\ s;
* extraction (esim-nc6.3) reads each chunk into typed
  :class:`~enterprise_sim.reconstruct.schema.MentionSpan`\\ s and
  :class:`~enterprise_sim.reconstruct.schema.CandidateTriple`\\ s (both still keyed
  by *surface form*, not entity);
* resolution (esim-nc6.4) clusters the mentions into canonical typed
  :class:`~enterprise_sim.core.world.Node`\\ s and hands back a ``mention →
  canonical-id`` map.

This module closes the loop. It rewrites every candidate triple's ``(src, rel,
dst)`` surface forms over the resolved canonical ids, **deduplicates** the results
into one edge per ``(src, rel, dst)`` — carrying a *support count* (how many chunks
attested it) and *provenance* (which chunks / artifacts), and gating on an
aggregated *confidence* that is the precision/recall knob — and assembles the
canonical nodes + aggregated edges + provenance into a
:class:`~enterprise_sim.reconstruct.schema.ReconstructedKG`. :func:`run_pipeline`
runs chunk → extract → resolve → aggregate end to end, and the built KG is
**persisted once** (:meth:`ReconstructedKG.write`) as the artifact every downstream
question (fidelity, reasoning) reuses — never rebuilt per query.

Everything here is a pure, deterministic function of the pipeline's inputs: the
same chunks, extractions, and resolution always aggregate to the same edges and
ids. The gated LLM steps live upstream (extraction, adjudication); with the
deterministic ``fake`` backend the whole pipeline still emits a (small) KG with no
key, so persistence and the ``reconstruct build`` CLI are testable in keyless CI.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.reconstruct.chunk import chunk_run
from enterprise_sim.reconstruct.extract import HAIKU_MODEL, Extraction, extract_chunks
from enterprise_sim.reconstruct.resolve import (
    DEFAULT_CONFIG,
    Resolution,
    ResolutionConfig,
    resolve_entities,
)
from enterprise_sim.reconstruct.schema import (
    CandidateTriple,
    Chunk,
    Provenance,
    ReconstructedKG,
)

__all__ = [
    "BuildConfig",
    "PipelineExtraction",
    "aggregate_relations",
    "build_kg",
    "extract_once",
    "project_with_groundings",
    "run_pipeline",
]

#: The node type used for artifacts, both in the gold KG and in the synthesized
#: grounding nodes below (matches the ``Artifact`` producers' ``N_ARTIFACT``).
_ARTIFACT_TYPE = "Artifact"


#: A reconstruction carries no real sim-time, so its nodes/edges are stamped with a
#: fixed epoch — keeping the build a pure function of its inputs (``created_at`` is
#: not scored by the fidelity scorer, which aligns on id / type / name).
_RECONSTRUCTED_AT = datetime(1970, 1, 1, tzinfo=UTC)

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Casefold and collapse whitespace so surface forms compare canonically.

    Matches the resolver's and fidelity scorer's normalization so a triple's
    endpoint surface form aligns with the mention surface form it names.
    """
    return _WS.sub(" ", text.strip()).casefold()


@dataclass(frozen=True)
class BuildConfig:
    """Knobs for the aggregation + build stage.

    Attributes:
        edge_confidence_threshold: Minimum aggregated confidence for an edge to be
            kept — the precision/recall knob. An edge's aggregated confidence is the
            greatest confidence among the candidate triples that attest it, so an
            edge stated confidently by *any* chunk clears a given bar. The default
            (``0.0``) keeps every resolvable edge; raising it trades recall for
            precision. Applied after endpoint resolution and dedup.
        resolution: The :class:`~enterprise_sim.reconstruct.resolve.ResolutionConfig`
            handed to the entity-resolution stage in :func:`run_pipeline`.
    """

    edge_confidence_threshold: float = 0.0
    resolution: ResolutionConfig = field(default_factory=lambda: DEFAULT_CONFIG)


DEFAULT_BUILD_CONFIG = BuildConfig()


# --------------------------------------------------------------------------- #
# Relation aggregation.
# --------------------------------------------------------------------------- #


def _mention_lookup(resolution: Resolution) -> dict[tuple[str, str], str]:
    """Map ``(chunk_id, normalized-surface-form) → canonical entity id``.

    The bridge from a candidate triple's surface-form endpoints back to resolved
    entities: a triple names its endpoints by the surface forms of mentions in the
    *same* chunk (its provenance), so an endpoint resolves by looking up that
    chunk's mention of the same normalized form. When two same-chunk mentions of
    one form resolved to different entities (distinct types), the lexicographically
    smallest id is chosen so the map is independent of mention order.
    """
    candidates: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entity in resolution.entities:
        for mention in entity.mentions:
            key = (mention.chunk_id, _normalize(mention.surface_form))
            candidates[key].add(entity.id)
    return {key: min(ids) for key, ids in candidates.items()}


def _edge_id(rel: str, src: str, dst: str) -> str:
    """A deterministic, unique edge id for a ``(src, rel, dst)`` triple."""
    return f"edge:{rel}:{src}:{dst}"


@dataclass
class _EdgeAccumulator:
    """Running aggregate for one ``(src, rel, dst)`` key across candidate triples."""

    support: int = 0
    confidence: float = 0.0
    chunk_ids: set[str] = field(default_factory=set)
    source_paths: set[str] = field(default_factory=set)


def aggregate_relations(
    extractions: Iterable[Extraction],
    resolution: Resolution,
    chunks: Iterable[Chunk] = (),
    *,
    threshold: float = 0.0,
) -> tuple[list[Edge], list[Provenance]]:
    """Rewrite candidate triples over canonical ids and dedupe into aggregated edges.

    Each :class:`~enterprise_sim.reconstruct.schema.CandidateTriple` has its
    ``src``/``dst`` surface forms mapped to canonical entity ids via
    ``resolution`` (endpoints that don't resolve, and self-loops, are dropped), and
    triples sharing a ``(src, rel, dst)`` are collapsed into one :class:`Edge`
    carrying ``props["support"]`` (attesting-chunk count) and ``props["confidence"]``
    (the greatest attesting confidence). Edges whose aggregated confidence is below
    ``threshold`` are dropped. Returns the edges and their provenance, both sorted
    by id — a pure function of the inputs.
    """
    lookup = _mention_lookup(resolution)
    source_of = {chunk.id: chunk.source_path for chunk in chunks}

    accumulators: dict[tuple[str, str, str], _EdgeAccumulator] = {}
    for extraction in extractions:
        for triple in extraction.triples:
            key = _resolve_triple(triple, lookup)
            if key is None:
                continue
            acc = accumulators.setdefault(key, _EdgeAccumulator())
            acc.support += 1
            acc.confidence = max(acc.confidence, triple.confidence)
            acc.chunk_ids.add(triple.provenance)
            source = source_of.get(triple.provenance)
            if source is not None:
                acc.source_paths.add(source)

    edges: list[Edge] = []
    provenance: list[Provenance] = []
    for (src, rel, dst), acc in accumulators.items():
        if acc.confidence < threshold:
            continue
        edge_id = _edge_id(rel, src, dst)
        edges.append(
            Edge(
                id=edge_id,
                type=rel,
                src=src,
                dst=dst,
                created_at=_RECONSTRUCTED_AT,
                props={"support": acc.support, "confidence": acc.confidence},
            )
        )
        provenance.append(
            Provenance(
                target_id=edge_id,
                chunk_ids=tuple(sorted(acc.chunk_ids)),
                source_paths=tuple(sorted(acc.source_paths)),
            )
        )
    edges.sort(key=lambda e: e.id)
    provenance.sort(key=lambda p: p.target_id)
    return edges, provenance


def _resolve_triple(
    triple: CandidateTriple,
    lookup: Mapping[tuple[str, str], str],
) -> tuple[str, str, str] | None:
    """Map a triple's endpoints to canonical ids, or ``None`` if it can't be an edge.

    Both endpoints must resolve (via a same-chunk mention) and be distinct; a
    self-loop or an unresolved endpoint yields ``None``.
    """
    src = lookup.get((triple.provenance, _normalize(triple.src_mention)))
    dst = lookup.get((triple.provenance, _normalize(triple.dst_mention)))
    if src is None or dst is None or src == dst:
        return None
    return src, triple.rel, dst


# --------------------------------------------------------------------------- #
# KG assembly.
# --------------------------------------------------------------------------- #


def _node_provenance(resolution: Resolution, chunks: Iterable[Chunk]) -> list[Provenance]:
    """One :class:`Provenance` per canonical node: its mentions' chunks + artifacts."""
    source_of = {chunk.id: chunk.source_path for chunk in chunks}
    records: list[Provenance] = []
    for entity in resolution.entities:
        chunk_ids = {mention.chunk_id for mention in entity.mentions}
        source_paths = {source_of[cid] for cid in chunk_ids if cid in source_of}
        records.append(
            Provenance(
                target_id=entity.id,
                chunk_ids=tuple(sorted(chunk_ids)),
                source_paths=tuple(sorted(source_paths)),
            )
        )
    return records


def build_kg(
    chunks: Sequence[Chunk],
    extractions: Iterable[Extraction],
    resolution: Resolution,
    *,
    config: BuildConfig = DEFAULT_BUILD_CONFIG,
) -> ReconstructedKG:
    """Assemble the persisted :class:`ReconstructedKG` from the pipeline's output.

    Canonical entities become gold-schema :class:`~enterprise_sim.core.world.Node`\\ s;
    candidate triples are aggregated into deduped :class:`~enterprise_sim.core.world.Edge`\\ s
    (:func:`aggregate_relations`, gated by ``config.edge_confidence_threshold``); and
    every node and edge gets a :class:`Provenance` record back to the chunks and
    artifacts that produced it. Pure and deterministic: the same inputs always yield
    the same KG, and :meth:`ReconstructedKG.write` sorts on write for byte-stability.
    """
    nodes: list[Node] = resolution.nodes(_RECONSTRUCTED_AT)
    edges, edge_provenance = aggregate_relations(
        extractions,
        resolution,
        chunks,
        threshold=config.edge_confidence_threshold,
    )
    provenance = _node_provenance(resolution, chunks) + edge_provenance
    return ReconstructedKG(nodes=nodes, edges=edges, provenance=provenance)


@dataclass(frozen=True)
class PipelineExtraction:
    """The build-once intermediate: the pipeline output *before* threshold gating.

    Chunking, extraction, and resolution are the pipeline's expensive, gated stages
    (extraction + adjudication call an LLM); aggregation is a pure, cheap function of
    their output plus a :class:`BuildConfig`. Capturing chunks + extractions +
    resolution once lets the caller build the KG at *many* edge-confidence thresholds
    without re-running the LLM — the "extract once, re-threshold many" primitive the
    :mod:`~enterprise_sim.reconstruct.sweep` harness runs on. Produced by
    :func:`extract_once`; :meth:`build` runs the final aggregate stage.

    Attributes:
        chunks: The chunked corpus (carries each chunk's ``source_path`` for provenance).
        extractions: Per-chunk typed mentions + candidate triples.
        resolution: The canonical entities the mentions resolved to.
    """

    chunks: list[Chunk]
    extractions: list[Extraction]
    resolution: Resolution

    def build(self, *, config: BuildConfig = DEFAULT_BUILD_CONFIG) -> ReconstructedKG:
        """Aggregate this extraction into a :class:`ReconstructedKG` under ``config``.

        A pure, deterministic re-run of the final aggregate stage
        (:func:`build_kg`): only ``config.edge_confidence_threshold`` (the
        precision/recall knob) varies the result — resolution is already fixed — so
        the same extraction builds a different KG at every threshold with no LLM call.
        """
        return build_kg(self.chunks, self.extractions, self.resolution, config=config)


def extract_once(
    run_dir: str,
    client: LLMClient,
    *,
    model: str | None = HAIKU_MODEL,
    resolution: ResolutionConfig = DEFAULT_CONFIG,
) -> PipelineExtraction:
    """Run chunk → extract → resolve over a run's corpus, stopping before aggregation.

    The gated, expensive prefix of :func:`run_pipeline`: chunks the raw artifact
    corpus under ``run_dir`` (never the gold ``kg/``), extracts typed mentions +
    candidate triples through ``client``, and resolves the mentions into canonical
    entities (``client`` gating the ambiguous-band adjudication). Returns the
    :class:`PipelineExtraction` the aggregate stage consumes, so a caller can build
    the KG at multiple edge-confidence thresholds from one extraction.
    """
    chunks = chunk_run(run_dir)
    extractions = list(extract_chunks(chunks, client, model=model))
    mentions = [mention for extraction in extractions for mention in extraction.mentions]
    canonical = resolve_entities(
        mentions,
        chunks,
        config=resolution,
        client=client,
        model=model,
    )
    return PipelineExtraction(chunks=list(chunks), extractions=extractions, resolution=canonical)


def run_pipeline(
    run_dir: str,
    client: LLMClient,
    *,
    model: str | None = HAIKU_MODEL,
    config: BuildConfig = DEFAULT_BUILD_CONFIG,
) -> ReconstructedKG:
    """Run chunk → extract → resolve → aggregate end to end over a run's corpus.

    Chunks the raw artifact corpus under ``run_dir`` (never the gold ``kg/``),
    extracts typed mentions + candidate triples from every chunk through ``client``,
    resolves the mentions into canonical entities (with ``client`` gating the
    ambiguous-band adjudication), and aggregates the triples into the persisted
    :class:`ReconstructedKG`. ``client`` is the caller's backend choice: a ``fake``
    client reconstructs a small KG deterministically with no key; an
    ``anthropic_api`` client with ``model`` (default :data:`HAIKU_MODEL`) runs the
    real gated extraction. Splits into :func:`extract_once` (the gated prefix) plus
    :meth:`PipelineExtraction.build` (the pure aggregate stage).
    """
    extraction = extract_once(run_dir, client, model=model, resolution=config.resolution)
    return extraction.build(config=config)


def project_with_groundings(kg: ReconstructedKG) -> tuple[World, dict[str, list[str]]]:
    """Load ``kg`` into a queryable world plus the grounding map the projection needs.

    The reconstruction persists *which artifacts ground each entity* as node
    :class:`~enterprise_sim.reconstruct.schema.Provenance` records
    (:meth:`~enterprise_sim.reconstruct.schema.ReconstructedKG.entity_groundings`),
    but the benchmark's provenance family is answered over derived ``mentions`` edges
    that :meth:`~enterprise_sim.benchmark.runners.projection.GraphModel.from_world`
    only mints from a ``{entity id → artifact node ids}`` map whose artifact endpoints
    **exist as nodes**. This bridges the two: it resolves each grounding artifact path
    to an existing ``Artifact`` node (matched by its ``path`` prop) or, when the
    reconstruction never surfaced that artifact as an entity, synthesizes a minimal
    ``Artifact`` node keyed by the path so the grounding edge has a real endpoint.

    Returns the augmented :class:`~enterprise_sim.core.world.World` (the reconstructed
    graph plus any synthesized artifact nodes) and the ``{entity id → artifact node
    ids}`` grounding map, ready for ``GraphModel.from_world(world, groundings)``.
    Pure and deterministic: the same KG always yields the same world and map.
    """
    world = kg.to_world()
    path_to_id: dict[str, str] = {}
    for node in world.nodes():
        if node.type == _ARTIFACT_TYPE:
            path = node.props.get("path")
            if isinstance(path, str) and path:
                path_to_id.setdefault(path, node.id)

    groundings: dict[str, list[str]] = {}
    for entity_id, paths in kg.entity_groundings().items():
        artifact_ids: list[str] = []
        for path in paths:
            artifact_id = path_to_id.get(path)
            if artifact_id is None:
                artifact_id = path
                path_to_id[path] = artifact_id
                if world.get_node(artifact_id) is None:
                    world.add_node(
                        Node(
                            id=artifact_id,
                            type=_ARTIFACT_TYPE,
                            created_at=_RECONSTRUCTED_AT,
                            props={"path": path},
                        )
                    )
            artifact_ids.append(artifact_id)
        if artifact_ids:
            groundings[entity_id] = artifact_ids
    return world, groundings
