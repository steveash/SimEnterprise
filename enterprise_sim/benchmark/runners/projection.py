"""The canonical graph projection the query engines load (esim-uzc.4).

Both embedded engines — Cypher (Kùzu) and SPARQL (Oxigraph) — consume one shared,
engine-neutral view of the gold knowledge graph: a :class:`GraphModel` of typed
nodes (each with a derived display label) and reified edges. This mirrors the
``GraphModel`` the graph-explorer sidecar builds in TypeScript, so the Python
engines load exactly what the reference implementation does.

Two deliberate choices:

* **Label derivation matches the sidecar.** :func:`derive_label` prefers the same
  property keys (``title``/``name``/``statement``/``issue_key``/``kind``) then the
  canonical alias, then the id — so a node renders identically here and in the
  explorer.
* **Mentions become first-class edges.** The gold answer key records which
  artifact *mentions* each entity (``kg/mentions.jsonl``); :meth:`GraphModel.from_world`
  projects those as ``mentions`` edges (artifact → entity) so the provenance
  reasoning family is answerable from the graph itself, not only from the raw
  corpus. Asserted graph edges are projected unchanged.

Projection is deterministic: nodes and edges sort by id, mention edges are
derived in sorted order, so the same gold run always yields the same model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from enterprise_sim.core.world import World

# The edge type minted for answer-key mentions (artifact → entity). Kept distinct
# from any asserted edge type so a runner can tell "the graph asserts this" from
# "the answer key grounds this".
MENTIONS_EDGE_TYPE = "mentions"

# Property keys, in priority order, that yield a node's human-readable label.
# Mirrors deriveLabel() in apps/graph-explorer/src/sidecar/graph/loader.ts.
_LABEL_KEYS = ("title", "name", "statement", "issue_key", "kind")


def derive_label(props: Mapping[str, Any], aliases: list[str], node_id: str) -> str:
    """Best human-readable label for a node (sidecar ``deriveLabel`` parity)."""
    for key in _LABEL_KEYS:
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if aliases and aliases[0].strip():
        return aliases[0]
    return node_id


@dataclass(frozen=True)
class ModelNode:
    """A typed node in the engine-neutral projection.

    Attributes:
        id: The gold KG node id (the unit of grading).
        type: The node label / class (``Person``, ``Artifact`` …).
        label: A derived display label (see :func:`derive_label`).
        created_at: Sim-time the node came into existence (ISO-8601 string).
        props: The node's scalar/structured properties.
        aliases: Known surface forms (canonical name first).
    """

    id: str
    type: str
    label: str
    created_at: str
    props: dict[str, Any] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelEdge:
    """A typed, reified edge in the engine-neutral projection.

    Attributes:
        id: The edge id (stable; minted for derived ``mentions`` edges).
        type: The relationship label (``reports_to``, ``mentions`` …).
        src: Source node id.
        dst: Destination node id.
        created_at: Sim-time the edge came into existence (ISO-8601 string).
        props: The edge's properties.
    """

    id: str
    type: str
    src: str
    dst: str
    created_at: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphModel:
    """The shared, engine-neutral view both query engines load.

    Holds the projected nodes and edges plus the sorted distinct node/edge types
    (handy for schema descriptions). Build one with :meth:`from_world`.
    """

    nodes: tuple[ModelNode, ...]
    edges: tuple[ModelEdge, ...]
    node_types: tuple[str, ...]
    edge_types: tuple[str, ...]

    @classmethod
    def from_world(
        cls,
        world: World,
        groundings: Mapping[str, list[str]] | None = None,
    ) -> GraphModel:
        """Project ``world`` (and optional answer-key ``groundings``) into a model.

        Asserted nodes and edges are projected from the gold :class:`World`. When
        ``groundings`` is given (entity id → artifact ids that mention it, as
        produced by :func:`enterprise_sim.benchmark.generate.load_groundings`), one
        ``mentions`` edge per (artifact, entity) pair is added so provenance is
        answerable from the graph. Everything is built in sorted order for
        determinism.
        """
        nodes = tuple(
            ModelNode(
                id=node.id,
                type=node.type,
                label=derive_label(node.props, node.aliases, node.id),
                created_at=node.created_at.isoformat(),
                props=dict(node.props),
                aliases=tuple(node.aliases),
            )
            for node in sorted(world.nodes(), key=lambda n: n.id)
        )

        edges = [
            ModelEdge(
                id=edge.id,
                type=edge.type,
                src=edge.src,
                dst=edge.dst,
                created_at=edge.created_at.isoformat(),
                props=dict(edge.props),
            )
            for edge in sorted(world.edges(), key=lambda e: e.id)
        ]
        edges.extend(_mention_edges(world, groundings or {}))

        node_types = tuple(sorted({n.type for n in nodes}))
        edge_types = tuple(sorted({e.type for e in edges}))
        return cls(nodes=nodes, edges=tuple(edges), node_types=node_types, edge_types=edge_types)


def _mention_edges(world: World, groundings: Mapping[str, list[str]]) -> list[ModelEdge]:
    """Derive ``mentions`` edges (artifact → entity) from the answer-key groundings.

    Each artifact that grounds an entity yields one edge ``artifact -mentions-> entity``.
    Only pairs whose endpoints both exist as nodes are emitted; the artifact's
    ``created_at`` stamps the edge. Emitted in sorted (entity, artifact) order.
    """
    out: list[ModelEdge] = []
    for entity_id in sorted(groundings):
        if entity_id not in world:
            continue
        for artifact_id in sorted(set(groundings[entity_id])):
            artifact = world.get_node(artifact_id)
            if artifact is None:
                continue
            out.append(
                ModelEdge(
                    id=f"{MENTIONS_EDGE_TYPE}:{artifact_id}:{entity_id}",
                    type=MENTIONS_EDGE_TYPE,
                    src=artifact_id,
                    dst=entity_id,
                    created_at=artifact.created_at.isoformat(),
                )
            )
    return out
