"""Producer plugins: events → concrete artifacts (ARCHITECTURE.md §4 Registry-4, §16).

A producer is a pure function of ``(Event, WorldView)`` that renders one or more
files and returns the knowledge-graph facts they express (nodes, edges, mentions,
provenance, validation issues). The v1 :class:`MarkdownProducer` is the default
the binding map routes every deliverable kind to; ``word``/``pptx``/``jira``
producers register alongside it later without touching ``core/``.

Shared, format-free building blocks live in
:mod:`enterprise_sim.producers.artifact` (result value types) and
:mod:`enterprise_sim.producers.grounding` (roster, mention tagger, repair detect).
"""

from __future__ import annotations

from enterprise_sim.producers.artifact import (
    Locator,
    Mention,
    ProducedArtifact,
    ValidationIssue,
    apply_to_world,
    issue_records,
    mention_records,
    provenance_records,
)
from enterprise_sim.producers.grounding import (
    DEFAULT_NAMED_TYPES,
    Roster,
    RosterEntry,
    detect_unresolved_names,
    tag_mentions,
)
from enterprise_sim.producers.markdown import MarkdownProducer, ProducerContext
from enterprise_sim.producers.pptx import PptxProducer, Slide, build_kickoff_deck

__all__ = [
    "DEFAULT_NAMED_TYPES",
    "Locator",
    "MarkdownProducer",
    "Mention",
    "PptxProducer",
    "ProducedArtifact",
    "ProducerContext",
    "Roster",
    "RosterEntry",
    "Slide",
    "ValidationIssue",
    "build_kickoff_deck",
    "apply_to_world",
    "detect_unresolved_names",
    "issue_records",
    "mention_records",
    "provenance_records",
    "tag_mentions",
]
