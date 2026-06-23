"""Producer plugins: events → concrete artifacts (ARCHITECTURE.md §4 Registry-4, §16).

A producer is a pure function of ``(Event, WorldView)`` that renders one or more
files and returns the knowledge-graph facts they express (nodes, edges, mentions,
provenance, validation issues). The v1 :class:`MarkdownProducer` is the default
the binding map routes every deliverable kind to; :class:`OutlookProducer` renders
the email/meeting kinds to ``.eml`` threads + ``.ics`` invites, :class:`WordProducer`
renders the document kinds to ``.docx`` with native threaded comments, and
:class:`JiraProducer` renders issue kinds (and a fan-out ``backlog``) to Jira-style
issue JSON — each registering alongside the others without touching ``core/``.

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
from enterprise_sim.producers.calendar_ics import (
    Attendee,
    Calendar,
    Meeting,
    render_calendar,
)
from enterprise_sim.producers.email_eml import (
    EmailMessage,
    EmailThread,
    Participant,
    render_message,
    render_thread,
)
from enterprise_sim.producers.grounding import (
    DEFAULT_NAMED_TYPES,
    Roster,
    RosterEntry,
    detect_unresolved_names,
    tag_mentions,
)
from enterprise_sim.producers.jira import JiraProducer
from enterprise_sim.producers.markdown import MarkdownProducer, ProducerContext
from enterprise_sim.producers.outlook import OutlookProducer
from enterprise_sim.producers.pptx import PptxProducer, Slide, build_kickoff_deck
from enterprise_sim.producers.word import WordProducer
from enterprise_sim.producers.word_docx import DocxComment, DocxDocument, build_docx

__all__ = [
    "DEFAULT_NAMED_TYPES",
    "Attendee",
    "Calendar",
    "DocxComment",
    "DocxDocument",
    "EmailMessage",
    "EmailThread",
    "JiraProducer",
    "Locator",
    "MarkdownProducer",
    "Meeting",
    "Mention",
    "OutlookProducer",
    "Participant",
    "PptxProducer",
    "ProducedArtifact",
    "ProducerContext",
    "Roster",
    "RosterEntry",
    "Slide",
    "ValidationIssue",
    "WordProducer",
    "build_docx",
    "build_kickoff_deck",
    "apply_to_world",
    "detect_unresolved_names",
    "issue_records",
    "mention_records",
    "provenance_records",
    "render_calendar",
    "render_message",
    "render_thread",
    "tag_mentions",
]
