"""Intermediate + final artifacts for the reconstruct pipeline (esim-nc6.1).

The *reconstruct* pipeline is the mirror image of the sim: instead of projecting a
gold knowledge graph *into* a corpus, it reads the raw corpus back *out* into a
reconstructed KG, then measures how faithfully the reconstruction recovers the
gold graph (epic esim-nc6). This module defines the data that flows through the
pipeline's stages, from raw text to a loadable graph:

* :class:`Chunk` — a unit of text carved from one source artifact, carrying a
  locator back to it (produced by the hierarchical chunker, esim-nc6.2).
* :class:`MentionSpan` — a surface-form entity mention located within a chunk
  (produced by mention detection / entity linking).
* :class:`CandidateTriple` — a proposed ``(src_mention, rel, dst_mention)``
  relation with the chunk that evidences it (produced by relation extraction).
* :class:`Provenance` — links a reconstructed node/edge id back to the chunks and
  source artifacts that produced it (the reconstruction's own answer key).
* :class:`ReconstructedKG` — the pipeline's terminal artifact. It writes
  ``nodes.jsonl`` / ``edges.jsonl`` in the **exact same schema as the gold KG**
  (:class:`enterprise_sim.core.world.Node` / :class:`~enterprise_sim.core.world.Edge`),
  so the benchmark's graph engines
  (:mod:`enterprise_sim.benchmark.runners.projection` /
  :mod:`~enterprise_sim.benchmark.runners.engines`) load a reconstruction with
  zero changes — plus a ``provenance.jsonl`` sidecar. Everything round-trips
  byte-stably through :meth:`~ReconstructedKG.write` / :meth:`~ReconstructedKG.read`.

Reusing the gold :class:`Node` / :class:`Edge` dataclasses (rather than defining a
parallel pair) is deliberate: it makes "writes the same schema" a type-level
guarantee instead of a convention the two ends must keep in sync.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.exporters.jsonl import write_jsonl

__all__ = [
    "CandidateTriple",
    "Chunk",
    "MentionSpan",
    "Provenance",
    "ReconstructedKG",
]


@dataclass(frozen=True)
class Chunk:
    """A unit of text carved from one source artifact, with a locator back to it.

    Attributes:
        id: Stable, content-derived identifier for the chunk.
        text: The chunk's raw text.
        source_path: The artifact path the chunk was carved from (matches an
            ``Artifact`` node's ``path`` prop, so a chunk resolves to its source).
        offset: Character offset of ``text`` within the source artifact.
        length: Character length of the chunk in the source; ``None`` means "the
            length of ``text``" (see :attr:`span_length`).
        section: Optional hierarchical section label (e.g. a markdown heading path
            or Jira field), set by the hierarchical chunker (esim-nc6.2).
    """

    id: str
    text: str
    source_path: str
    offset: int = 0
    length: int | None = None
    section: str | None = None

    @property
    def span_length(self) -> int:
        """The chunk's length in the source (``length`` if set, else ``len(text)``)."""
        return self.length if self.length is not None else len(self.text)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "id": self.id,
            "text": self.text,
            "source_path": self.source_path,
            "offset": self.offset,
            "length": self.length,
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Chunk:
        """Reconstruct a :class:`Chunk` from :meth:`to_dict` output."""
        return cls(
            id=data["id"],
            text=data["text"],
            source_path=data["source_path"],
            offset=data.get("offset", 0),
            length=data.get("length"),
            section=data.get("section"),
        )


@dataclass(frozen=True)
class MentionSpan:
    """A surface-form entity mention located within a :class:`Chunk`.

    Attributes:
        chunk_id: The :class:`Chunk` this mention was found in.
        surface_form: The exact text of the mention.
        start: Character offset of the mention within the chunk's ``text``, or
            ``-1`` when the surface form could not be located in the chunk (the
            extractor named an entity it did not quote verbatim).
        end: Character offset just past the mention within the chunk's ``text``
            (``-1`` when unlocated; see ``start``).
        entity_type: The ontology entity type the extractor assigned this mention
            (one of :data:`~enterprise_sim.reconstruct.ontology.NODE_TYPES`), or
            ``None`` if untyped. Entity resolution (esim-nc6.4) uses it to type the
            reconstructed :class:`~enterprise_sim.core.world.Node`.
        entity_id: The reconstructed entity id this mention was linked to, or
            ``None`` before entity linking has run.
    """

    chunk_id: str
    surface_form: str
    start: int
    end: int
    entity_type: str | None = None
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "chunk_id": self.chunk_id,
            "surface_form": self.surface_form,
            "start": self.start,
            "end": self.end,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MentionSpan:
        """Reconstruct a :class:`MentionSpan` from :meth:`to_dict` output."""
        return cls(
            chunk_id=data["chunk_id"],
            surface_form=data["surface_form"],
            start=data["start"],
            end=data["end"],
            entity_type=data.get("entity_type"),
            entity_id=data.get("entity_id"),
        )


@dataclass(frozen=True)
class CandidateTriple:
    """A proposed relation between two mentions, with the chunk that evidences it.

    Attributes:
        src_mention: Surface form of the relation's source entity.
        rel: The relation label (aligned with the gold edge types where possible).
        dst_mention: Surface form of the relation's destination entity.
        provenance: The :class:`Chunk` id the triple was extracted from.
        confidence: Extractor confidence in ``[0, 1]`` (default ``1.0``).
    """

    src_mention: str
    rel: str
    dst_mention: str
    provenance: str
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "src_mention": self.src_mention,
            "rel": self.rel,
            "dst_mention": self.dst_mention,
            "provenance": self.provenance,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CandidateTriple:
        """Reconstruct a :class:`CandidateTriple` from :meth:`to_dict` output."""
        return cls(
            src_mention=data["src_mention"],
            rel=data["rel"],
            dst_mention=data["dst_mention"],
            provenance=data["provenance"],
            confidence=data.get("confidence", 1.0),
        )


@dataclass(frozen=True)
class Provenance:
    """Links a reconstructed KG element back to the evidence that produced it.

    The reconstruction's own answer key: for each reconstructed node or edge id,
    the chunks — and the artifacts those chunks came from — that support it. Its
    ``target_id``-keyed shape parallels the gold ``provenance.jsonl`` so the two
    answer keys can be compared by the fidelity scorer (esim-nc6.6).

    Attributes:
        target_id: The reconstructed node or edge id this record explains.
        chunk_ids: The :class:`Chunk` ids that evidence the target.
        source_paths: The artifact paths those chunks were carved from.
    """

    target_id: str
    chunk_ids: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (tuples rendered as lists)."""
        return {
            "target_id": self.target_id,
            "chunk_ids": list(self.chunk_ids),
            "source_paths": list(self.source_paths),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Provenance:
        """Reconstruct a :class:`Provenance` from :meth:`to_dict` output."""
        return cls(
            target_id=data["target_id"],
            chunk_ids=tuple(data.get("chunk_ids", [])),
            source_paths=tuple(data.get("source_paths", [])),
        )


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each non-blank line of a JSONL file at ``path`` as a parsed dict."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


@dataclass
class ReconstructedKG:
    """The pipeline's terminal artifact: a KG in the gold on-disk format.

    Holds reconstructed :class:`~enterprise_sim.core.world.Node`\\ s and
    :class:`~enterprise_sim.core.world.Edge`\\ s — the *same* dataclasses the gold
    KG uses — plus a :class:`Provenance` answer key. :meth:`write` emits
    ``nodes.jsonl`` / ``edges.jsonl`` byte-identical in schema to a golden run's,
    so :func:`enterprise_sim.benchmark.generate.load_world_from_run` and the graph
    engines load a reconstruction unchanged; ``provenance.jsonl`` rides alongside.

    Files are written deterministically — nodes/edges sorted by id, provenance by
    ``target_id``, keys sorted, compact separators — so identical inputs yield
    byte-identical files (matching the gold exporter, D10).
    """

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)

    #: The files :meth:`write` emits / :meth:`read` expects, in the gold ``kg/`` layout.
    NODES_FILE = "nodes.jsonl"
    EDGES_FILE = "edges.jsonl"
    PROVENANCE_FILE = "provenance.jsonl"

    @property
    def node_count(self) -> int:
        """Number of reconstructed nodes."""
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        """Number of reconstructed edges."""
        return len(self.edges)

    def add_node(self, node: Node) -> Node:
        """Append a reconstructed node and return it."""
        self.nodes.append(node)
        return node

    def add_edge(self, edge: Edge) -> Edge:
        """Append a reconstructed edge and return it."""
        self.edges.append(edge)
        return edge

    def add_provenance(self, provenance: Provenance) -> Provenance:
        """Append a provenance record and return it."""
        self.provenance.append(provenance)
        return provenance

    def to_world(self) -> World:
        """Load the reconstruction into a :class:`~enterprise_sim.core.world.World`.

        The same in-memory graph the benchmark's
        :meth:`~enterprise_sim.benchmark.runners.projection.GraphModel.from_world`
        projects, so a reconstruction is queryable through the graph engines
        without a round-trip through disk.
        """
        world = World()
        for node in self.nodes:
            world.add_node(node)
        for edge in self.edges:
            world.add_edge(edge)
        return world

    def write(self, out_dir: str | Path) -> list[Path]:
        """Write ``nodes/edges/provenance.jsonl`` under ``out_dir``; return the files.

        ``out_dir`` is created if missing. Rows are emitted in sorted order for
        byte-stability; the node/edge files match the gold KG schema exactly.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        node_rows = [node.to_dict() for node in sorted(self.nodes, key=lambda n: n.id)]
        edge_rows = [edge.to_dict() for edge in sorted(self.edges, key=lambda e: e.id)]
        prov_rows = [rec.to_dict() for rec in sorted(self.provenance, key=lambda p: p.target_id)]
        return [
            write_jsonl(out / self.NODES_FILE, node_rows),
            write_jsonl(out / self.EDGES_FILE, edge_rows),
            write_jsonl(out / self.PROVENANCE_FILE, prov_rows),
        ]

    @classmethod
    def read(cls, in_dir: str | Path) -> ReconstructedKG:
        """Read a reconstruction written by :meth:`write` back from ``in_dir``.

        ``nodes.jsonl`` and ``edges.jsonl`` are required; ``provenance.jsonl`` is
        optional (absent ⇒ no provenance records).
        """
        src = Path(in_dir)
        nodes = [Node.from_dict(row) for row in _read_jsonl(src / cls.NODES_FILE)]
        edges = [Edge.from_dict(row) for row in _read_jsonl(src / cls.EDGES_FILE)]
        prov_path = src / cls.PROVENANCE_FILE
        provenance = (
            [Provenance.from_dict(row) for row in _read_jsonl(prov_path)]
            if prov_path.is_file()
            else []
        )
        return cls(nodes=nodes, edges=edges, provenance=provenance)
