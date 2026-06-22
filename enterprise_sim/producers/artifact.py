"""Shared value types every producer speaks (ARCHITECTURE.md §5, §11.3-11.4).

A producer is a *pure function of* ``(Event, WorldView)`` that emits one or more
concrete files plus the knowledge-graph facts those files express. This module
holds the small, immutable contracts that result is made of — kept format-free so
the ``word``/``pptx``/``jira`` producers of later milestones reuse them unchanged:

* :class:`Locator` — *where* in a rendered artifact a span lives. Per-medium
  (markdown → char offset/length/line); the schema is uniform so a consumer
  handles one shape (§11.3).
* :class:`Mention` — an entity surface form occurring in artifact text (the
  ``kg/mentions.jsonl`` row, §11.4): entity-recognition + coreference ground truth.
* :class:`ValidationIssue` — a soft consistency finding (``validation/issues.jsonl``,
  D17) — e.g. an unresolved name the grounding repair pass could not fix.
* :class:`ProducedArtifact` — the producer's return value: the rendered body, the
  ``Artifact`` :class:`~enterprise_sim.core.world.Node`, the relationship
  :class:`~enterprise_sim.core.world.Edge`\\ s it expresses (``authored`` /
  ``reviewed`` / ``expresses`` / ``references``), its mentions, and any issues.

The producer *returns* these rather than mutating a graph so the orchestration
layer decides when to apply them; :func:`apply_to_world` is the one-liner that
does so, and :func:`provenance_records` / :func:`mention_records` /
:func:`issue_records` serialize the §11.4 side files.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from enterprise_sim.core.world import Edge, Node, World

__all__ = [
    "Locator",
    "Mention",
    "ProducedArtifact",
    "ValidationIssue",
    "apply_to_world",
    "issue_records",
    "mention_records",
    "provenance_records",
]


@dataclass(frozen=True, slots=True)
class Locator:
    """*Where* a span sits in a rendered artifact (uniform across media, §11.3).

    For markdown (the v1 medium) a span is identified by its character ``offset``
    into the document, its ``length`` in characters, and the 1-based ``line`` it
    starts on. ``medium`` names the addressing scheme so a later docx producer can
    emit ``{"medium": "docx", "paragraph": .., "run": ..}`` against the same field.
    """

    offset: int
    length: int
    line: int
    medium: str = "markdown"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping stored in ``mentions.jsonl``."""
        return {
            "medium": self.medium,
            "offset": self.offset,
            "length": self.length,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class Mention:
    """One occurrence of an entity's surface form in an artifact (§11.3-11.4).

    A ``kg/mentions.jsonl`` row: the artifact it appears in, the entity it
    resolves to, the exact ``surface_form`` text, and its :class:`Locator`. Full
    occurrences are recorded (not just first-seen), so this is the coreference
    answer key for entity recognition.
    """

    artifact_path: str
    entity_id: str
    surface_form: str
    locator: Locator

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable ``mentions.jsonl`` row."""
        return {
            "artifact_path": self.artifact_path,
            "entity_id": self.entity_id,
            "surface_form": self.surface_form,
            "locator": self.locator.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A soft consistency finding kept beside the run (``issues.jsonl``, D17).

    Producers never fail a run on a content problem; they log it and keep the
    artifact (§16.2 step 3). ``kind`` is a stable machine tag
    (``"unresolved_mention"``); ``message`` is human-readable; ``where`` points at
    the offending artifact; ``details`` carries structured extras.
    """

    kind: str
    message: str
    where: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable ``issues.jsonl`` row."""
        return {
            "kind": self.kind,
            "message": self.message,
            "where": self.where,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class ProducedArtifact:
    """Everything one rendered file contributes to the corpus + the KG (§5).

    Attributes:
        artifact_id: Stable id of the ``Artifact`` KG node this file renders.
        path: Run-relative output path of the rendered file (e.g.
            ``artifacts/weekly-status-w12.md``).
        fmt: Concrete format tag (``"markdown"``).
        body: The rendered file contents.
        node: The ``Artifact`` :class:`Node` (created here if Layer B did not).
        edges: The reified relationships this file expresses — ``authored``,
            ``reviewed``, ``expresses`` (provenance), ``references`` (D16).
        mentions: Entity surface-form occurrences found by the tagger (§11.3).
        issues: Soft validation findings (D17), e.g. an unrepaired name.
        metadata: A JSON-serializable twin of the artifact (the ``markdown/json``
            deliverable): metadata + references + mention summary.
        binary_body: The rendered file as raw bytes, set by producers whose format
            is binary (e.g. the ``pptx``/``docx`` producers). When present it — not
            ``body`` — is what the runner writes to ``path``; ``body`` then holds a
            plain-text rendering the tagger and grounding layers reason over.
    """

    artifact_id: str
    path: str
    fmt: str
    body: str
    node: Node
    edges: list[Edge] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    binary_body: bytes | None = None

    @property
    def is_binary(self) -> bool:
        """True when this artifact's on-disk form is :attr:`binary_body`, not ``body``."""
        return self.binary_body is not None

    def expressed_targets(self) -> list[str]:
        """Node and edge ids whose provenance includes this artifact (§11.3, D19).

        Provenance targets *both nodes and edges* — the whole point of reifying
        edges. This file is artifact-level evidence for:

        * the ``Artifact`` **node** it renders (``artifact_id``);
        * each subject **node** it expresses — the destination of every
          ``expresses`` edge (the entity the file is about);
        * each reified **relationship** it establishes — the ``authored`` /
          ``reviewed`` / ``references`` edge ids. An ``expresses`` edge is itself
          the provenance link (its source *is* this artifact), so it contributes
          its subject node rather than its own id, which would be circular.
        """
        targets = [self.artifact_id]
        for edge in self.edges:
            if edge.type == "expresses":
                targets.append(edge.dst)  # the subject node this file is evidence for
            else:
                targets.append(edge.id)  # the reified authored/reviewed/references edge
        return targets

    def references(self) -> list[str]:
        """Artifact ids this file cites (the ``references`` edge destinations, D16)."""
        return [e.dst for e in self.edges if e.type == "references"]


def apply_to_world(world: World, produced: Iterable[ProducedArtifact]) -> None:
    """Add every produced artifact's node + edges to ``world`` (idempotently).

    The ``Artifact`` node is added only if absent (Layer B may have created the
    abstract artifact already); edges are added only if their id is new, so
    applying the same producer output twice is a no-op rather than a duplicate-id
    error.
    """
    for art in produced:
        if world.get_node(art.node.id) is None:
            world.add_node(art.node)
        for edge in art.edges:
            if world.get_edge(edge.id) is None:
                world.add_edge(edge)


def provenance_records(produced: Iterable[ProducedArtifact]) -> list[dict[str, Any]]:
    """Invert produced artifacts into ``provenance.jsonl`` rows (§11.4, D19).

    Provenance is keyed by *target* (node or edge id) → the artifacts that express
    it; producers naturally carry the inverse (artifact → targets), so we pivot.
    Rows are sorted by ``target_id`` and each artifact list by path for stable,
    line-diffable output.

    Provenance is **artifact-level** in v1: each artifact entry is ``{"path": ...}``
    with the span ``locator`` field deliberately *reserved* (omitted) for a future
    span-level grounding pass — mirroring the ``{path, locator?}`` shape in §11.4.
    Mentions (:func:`mention_records`) already carry full span locators.
    """
    by_target: dict[str, list[str]] = {}
    for art in produced:
        for target in art.expressed_targets():
            paths = by_target.setdefault(target, [])
            if art.path not in paths:
                paths.append(art.path)
    return [
        {"target_id": target, "artifacts": [{"path": p} for p in sorted(paths)]}
        for target, paths in sorted(by_target.items())
    ]


def mention_records(produced: Iterable[ProducedArtifact]) -> list[dict[str, Any]]:
    """Flatten every artifact's mentions into ``mentions.jsonl`` rows (§11.4)."""
    rows: list[dict[str, Any]] = []
    for art in produced:
        rows.extend(m.to_dict() for m in art.mentions)
    return rows


def issue_records(produced: Iterable[ProducedArtifact]) -> list[dict[str, Any]]:
    """Flatten every artifact's validation issues into ``issues.jsonl`` rows (D17)."""
    rows: list[dict[str, Any]] = []
    for art in produced:
        rows.extend(i.to_dict() for i in art.issues)
    return rows


def aliases_for(node: Node) -> list[str]:
    """Return a node's known surface forms: canonical name first, then aliases.

    A small convenience the grounding layer and tests share so the canonical
    display name is always tagged even when it is not duplicated into ``aliases``.
    """
    names: list[str] = []
    name = node.props.get("name") or node.props.get("statement") or node.props.get("title")
    if isinstance(name, str) and name:
        names.append(name)
    for alias in node.aliases:
        if alias and alias not in names:
            names.append(alias)
    return names
