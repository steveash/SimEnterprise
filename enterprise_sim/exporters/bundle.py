"""The canonical Gold-KG bundle every exporter consumes (ARCHITECTURE.md §11.4).

JSONL is the **canonical** on-disk form: ``nodes`` / ``edges`` / ``events`` plus
the three answer-key side files (``provenance`` / ``mentions`` / ``aliases``).
:class:`KgBundle` is the in-memory mirror of exactly that set — a flat tuple of
already-serialized rows per file — so every exporter (the JSONL writer, the Neo4j
adapter, future RDF/GraphML adapters) reads one stable shape and the core stays
ignorant of any interop format (§11.5).

Holding plain JSON-ready dicts (rather than the live :class:`World`) keeps the
bundle trivially serializable and lets the round-trip sanity check reconstruct a
:class:`World` from ``nodes`` + ``edges`` and confirm byte-stable re-serialization
(the M7 acceptance check).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from enterprise_sim.core.world import Node, World
from enterprise_sim.producers.artifact import (
    aliases_for,
    mention_records,
    provenance_records,
)

if TYPE_CHECKING:
    from enterprise_sim.assembly.corpus import CorpusResult

__all__ = ["KgBundle", "aliases_records"]


def aliases_records(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive ``aliases.jsonl`` rows: ``{entity_id, canonical, aliases}`` (§11.3).

    The per-entity alias table is the union of an entity's known surface forms —
    its canonical name first, then any further aliases. Only nodes that carry a
    name contribute a row; rows are emitted in node (``id``) order for stable,
    line-diffable output.
    """
    rows: list[dict[str, Any]] = []
    for data in nodes:
        surfaces = aliases_for(Node.from_dict(data))
        if not surfaces:
            continue
        rows.append(
            {
                "entity_id": data["id"],
                "canonical": surfaces[0],
                "aliases": surfaces[1:],
            }
        )
    return rows


@dataclass(frozen=True, slots=True)
class KgBundle:
    """The canonical Gold-KG, as the flat per-file rows every exporter reads.

    Each attribute is the full, deterministically ordered row list of one
    ``kg/*.jsonl`` file (§11.4). Exporters never reach back into the live graph —
    everything they need is here — so an adapter for a new target format only has
    to map these six lists.

    Attributes:
        nodes: ``nodes.jsonl`` rows (``{id, type, props, created_at, aliases}``).
        edges: ``edges.jsonl`` rows (``{id, type, src, dst, props, created_at}``).
        events: ``events.jsonl`` rows — the append-only temporal journal (§11.2).
        provenance: ``provenance.jsonl`` rows (``{target_id, artifacts}``).
        mentions: ``mentions.jsonl`` rows (``{artifact_path, entity_id, …}``).
        aliases: ``aliases.jsonl`` rows (``{entity_id, canonical, aliases}``).
    """

    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    provenance: tuple[dict[str, Any], ...]
    mentions: tuple[dict[str, Any], ...]
    aliases: tuple[dict[str, Any], ...]

    @classmethod
    def from_run(cls, world: World, corpus: CorpusResult) -> KgBundle:
        """Assemble the canonical bundle from a finished run.

        ``nodes``/``edges`` are the materialized graph (sorted by id by
        :meth:`World.to_dict`); ``events`` is the combined scheduler journal in
        ``(timestamp, id)`` order; ``provenance``/``mentions`` are inverted from the
        produced artifacts; ``aliases`` is derived from the node surface forms.
        """
        graph = world.to_dict()
        nodes = list(graph["nodes"])
        return cls(
            nodes=tuple(nodes),
            edges=tuple(graph["edges"]),
            events=tuple(event.to_dict() for event in corpus.journal.ordered()),
            provenance=tuple(provenance_records(corpus.artifacts)),
            mentions=tuple(mention_records(corpus.artifacts)),
            aliases=tuple(aliases_records(nodes)),
        )

    # -- derived views ------------------------------------------------------

    def artifact_path_to_id(self) -> dict[str, str]:
        """Map each rendered artifact ``path`` to its ``Artifact`` node id.

        Provenance and mentions reference artifacts by run-relative *path* (the
        canonical, location-independent key); the Neo4j adapter needs the artifact
        *node* to attach ``:EXPRESSES`` / ``:MENTIONS`` relationships, so this
        inverts the ``Artifact`` nodes' ``props.path``.
        """
        mapping: dict[str, str] = {}
        for node in self.nodes:
            if node.get("type") != "Artifact":
                continue
            path = node.get("props", {}).get("path")
            if isinstance(path, str) and path:
                mapping[path] = node["id"]
        return mapping

    def graph_json(self) -> dict[str, Any]:
        """Return the convenience node-link form (``graph.json``, networkx-loadable).

        The shape matches :func:`networkx.node_link_data` for a directed multigraph:
        ``nodes`` keyed by ``id`` and ``links`` keyed by ``source``/``target``/``key``,
        every other field carried through as an attribute. It is a convenience view
        — the JSONL files remain canonical (§11.4).
        """
        nodes = [
            {
                "id": n["id"],
                "type": n["type"],
                "created_at": n["created_at"],
                "props": n["props"],
                "aliases": n["aliases"],
            }
            for n in self.nodes
        ]
        links = [
            {
                "source": e["src"],
                "target": e["dst"],
                "key": e["id"],
                "type": e["type"],
                "created_at": e["created_at"],
                "props": e["props"],
            }
            for e in self.edges
        ]
        return {
            "directed": True,
            "multigraph": True,
            "graph": {},
            "nodes": nodes,
            "links": links,
        }
