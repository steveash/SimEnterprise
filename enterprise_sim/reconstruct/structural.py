"""Deterministic structural extraction of org-chart / roster relations (esim-din.3).

The LLM extractor (:mod:`enterprise_sim.reconstruct.extract`) reads relations out
of *prose*, but the corpus's ``organization/`` reference data encodes the core org
relations **structurally** — in the layout of a roster table, a team heading, or a
project member list — rather than as sentences a reader would parse. A person's
row under a ``### Team`` heading *is* a ``member_of`` edge; the ⭐ on the lead's row
*is* a ``leads`` edge; the other rows *report_to* that lead. Those are the exact
relation types the reconstruction was recall-bound on (``member_of``, ``leads``,
``part_of``, ``reports_to``), and a language model reading one chunk at a time
routinely misses them because nothing in the text *states* them.

This module recovers them **deterministically, with no LLM** — a pure function of a
single :class:`~enterprise_sim.reconstruct.schema.Chunk`. It recognizes the three
org-markdown shapes the world builder emits
(:mod:`enterprise_sim.world_builders.markdown`) and reads the relations straight
off their structure:

* **Team roster** (``… > Teams > <Team>`` — a people table under a team heading):
  ``part_of`` (team → its department, from the breadcrumb), and per member row
  ``member_of`` (person → team), ``leads`` (the ⭐-marked lead → team), and
  ``reports_to`` (each non-lead member → the lead).
* **Project roster** (``… > Initiatives & projects`` — a ``📦 <Project>`` bullet
  with ``- <role>: <Person>`` children): ``member_of`` (each listed person →
  the project), whatever the role.
* **Department goals** (``<Department> > Advances goals`` — a bullet list of goal
  statements): ``advances_goal`` (department → each goal statement).

Every endpoint is emitted as a mention **in the same chunk** as its edge, because
the aggregate stage (:func:`enterprise_sim.reconstruct.build.aggregate_relations`)
resolves a triple's endpoints only through same-chunk mentions. The output is a raw
extraction *envelope* (the same shape the LLM backend returns), so it flows through
the identical :func:`~enterprise_sim.reconstruct.extract.parse_extraction` —
inheriting span location, ontology validation, confidence clamping, and dedup — and
merges with the LLM's own extraction for the chunk. Recognizers are keyed on the
specific breadcrumbs and row shapes the reference data uses, so a chunk that is not
org roster markdown yields an empty envelope and changes nothing.
"""

from __future__ import annotations

import re
from typing import Any

from enterprise_sim.reconstruct.schema import Chunk

__all__ = ["structural_envelope"]

#: The lead marker the roster renderer appends to a lead's name cell (a ⭐ star).
_LEAD_MARKER = "⭐"

#: The project bullet marker the initiatives renderer prefixes to a project name.
_PROJECT_MARKER = "📦"

#: Breadcrumb segment that opens the per-team roster tables under a department.
_TEAMS_SEGMENT = "Teams"

#: Breadcrumb tail marking a department's advanced-goals bullet list.
_ADVANCES_GOALS_TAIL = "Advances goals"

#: Breadcrumb tail marking the initiatives-and-projects bullet tree.
_INITIATIVES_TAIL = "Initiatives & projects"

#: A markdown table row: the cells between the outer pipes (``| a | b |``).
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")

#: A table separator row (``| --- | --- |``): only pipes, dashes, colons, spaces.
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")

#: A ``- role: Person`` project-member bullet (any indent); captures role + name.
_ROLE_MEMBER = re.compile(r"^\s*[-*]\s*([A-Za-z][\w /]*?):\s*(.+?)\s*$")

#: A plain ``- text`` bullet (any indent); captures the bullet body.
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def structural_envelope(chunk: Chunk) -> dict[str, list[dict[str, Any]]]:
    """Extract org relations laid out structurally in ``chunk``'s markdown.

    Returns a raw extraction envelope — ``{"mentions": [...], "triples": [...]}``,
    the same shape the LLM backend produces — so the caller can merge it with the
    model's extraction and run both through
    :func:`~enterprise_sim.reconstruct.extract.parse_extraction`. A chunk that
    matches none of the recognized org-markdown shapes yields empty lists, so this
    is a safe no-op on the rest of the corpus. Pure function of ``chunk``.
    """
    segments = _segments(chunk.section)
    mentions: list[dict[str, Any]] = []
    triples: list[dict[str, Any]] = []

    if _TEAMS_SEGMENT in segments and segments[-1] != _TEAMS_SEGMENT:
        _team_roster(chunk.text, segments, mentions, triples)
    elif segments and segments[-1] == _ADVANCES_GOALS_TAIL:
        _department_goals(chunk.text, segments, mentions, triples)
    elif segments and segments[-1] == _INITIATIVES_TAIL:
        _project_rosters(chunk.text, mentions, triples)

    return {"mentions": mentions, "triples": triples}


def _segments(section: str | None) -> list[str]:
    """Split a chunk's breadcrumb ``section`` into its heading segments."""
    if not section:
        return []
    return [part.strip() for part in section.split(" > ") if part.strip()]


def _mention(mentions: list[dict[str, Any]], surface_form: str, type_: str) -> None:
    """Append a mention (deduping is left to :func:`parse_extraction`)."""
    if surface_form:
        mentions.append({"surface_form": surface_form, "type": type_})


def _triple(triples: list[dict[str, Any]], src: str, rel: str, dst: str) -> None:
    """Append a high-confidence structural triple (both endpoints must be non-empty)."""
    if src and dst:
        triples.append({"src": src, "rel": rel, "dst": dst, "confidence": 1.0})


def _team_roster(
    text: str,
    segments: list[str],
    mentions: list[dict[str, Any]],
    triples: list[dict[str, Any]],
) -> None:
    """Read a ``… > Teams > <Team>`` roster table into org edges.

    The department is the breadcrumb root and the team its leaf. Each table data
    row is a member (``member_of`` → team); the ⭐-marked row is the lead
    (``leads`` → team) that the other members ``reports_to``; and the team is
    ``part_of`` its department.
    """
    department = segments[0]
    team = segments[-1]
    _mention(mentions, team, "Team")
    _mention(mentions, department, "Department")
    _triple(triples, team, "part_of", department)

    members = _roster_members(text)
    lead = next((name for name, is_lead in members if is_lead), None)
    for name, is_lead in members:
        _mention(mentions, name, "Person")
        _triple(triples, name, "member_of", team)
        if is_lead:
            _triple(triples, name, "leads", team)
        elif lead is not None:
            _triple(triples, name, "reports_to", lead)


def _roster_members(text: str) -> list[tuple[str, bool]]:
    """Return ``(name, is_lead)`` for each data row of the roster table in ``text``.

    Reads the first column (Name) of every markdown table row, skipping the header
    and separator rows, and strips the ⭐ lead marker — reporting the stripped name
    plus whether the marker was present.
    """
    members: list[tuple[str, bool]] = []
    seen_header = False
    for line in text.splitlines():
        if _TABLE_SEPARATOR.match(line):
            seen_header = True
            continue
        row = _TABLE_ROW.match(line)
        if row is None:
            continue
        if not seen_header:
            # The header row (``| Name | Title | … |``) precedes the separator.
            continue
        first_cell = row.group(1).split("|", 1)[0].strip()
        is_lead = _LEAD_MARKER in first_cell
        name = first_cell.replace(_LEAD_MARKER, "").strip()
        if name:
            members.append((name, is_lead))
    return members


def _department_goals(
    text: str,
    segments: list[str],
    mentions: list[dict[str, Any]],
    triples: list[dict[str, Any]],
) -> None:
    """Read a ``<Department> > Advances goals`` bullet list into ``advances_goal`` edges."""
    department = segments[0]
    _mention(mentions, department, "Department")
    for line in text.splitlines():
        bullet = _BULLET.match(line)
        if bullet is None:
            continue
        statement = bullet.group(1).strip()
        _mention(mentions, statement, "Goal")
        _triple(triples, department, "advances_goal", statement)


def _project_rosters(
    text: str,
    mentions: list[dict[str, Any]],
    triples: list[dict[str, Any]],
) -> None:
    """Read ``📦 <Project>`` bullets + ``- <role>: <Person>`` children into ``member_of`` edges.

    Walks the initiatives-and-projects bullet tree top to bottom: a ``📦`` line sets
    the current project, and each following role-tagged member bullet emits
    ``member_of`` (person → that project), whatever the role (lead / contributor /
    reviewer). Bullets before the first project are ignored.
    """
    current_project: str | None = None
    for line in text.splitlines():
        if _PROJECT_MARKER in line:
            current_project = line.split(_PROJECT_MARKER, 1)[1].strip()
            _mention(mentions, current_project, "Project")
            continue
        if current_project is None:
            continue
        member = _ROLE_MEMBER.match(line)
        if member is None:
            continue
        name = member.group(2).strip()
        _mention(mentions, name, "Person")
        _triple(triples, name, "member_of", current_project)
