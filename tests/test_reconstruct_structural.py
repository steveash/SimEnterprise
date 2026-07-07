"""Deterministic structural org-relation extraction tests (esim-din.3).

The reconstruction was recall-bound on the core org relations — ``member_of``,
``leads``, ``part_of``, ``reports_to`` — because a chunk-at-a-time LLM misses
relations the ``organization/`` reference markdown encodes *structurally* (a roster
table, a ⭐ lead marker, a project member list) rather than states in prose.
:mod:`enterprise_sim.reconstruct.structural` reads those off the layout with no
LLM, so every relation type has a **keyless** fixture here proving the edge is
recovered with the correct typed endpoints — plus the integration that merges the
structural envelope into :func:`extract_chunk` and the guarantee it no-ops on
non-roster chunks.
"""

from __future__ import annotations

from enterprise_sim.core.llm import LLMConfig, build_client
from enterprise_sim.reconstruct import Chunk, extract_chunk, parse_extraction
from enterprise_sim.reconstruct.extract import merge_envelopes
from enterprise_sim.reconstruct.structural import structural_envelope


def _chunk(text: str, section: str | None) -> Chunk:
    """A :class:`Chunk` with a stable id for structural-extraction tests."""
    return Chunk(
        id="chunk-1", text=text, source_path="organization/x.md", offset=0, section=section
    )


def _edges(chunk: Chunk) -> set[tuple[str, str, str]]:
    """The ``(src, rel, dst)`` triples the structural reader emits for ``chunk``.

    Routed through :func:`parse_extraction` exactly as ``extract_chunk`` does, so
    the surface forms are the located, validated mentions the pipeline consumes.
    """
    extraction = parse_extraction(chunk, structural_envelope(chunk))
    return {(t.src_mention, t.rel, t.dst_mention) for t in extraction.triples}


def _typed_mentions(chunk: Chunk) -> set[tuple[str, str | None]]:
    """The ``(surface_form, entity_type)`` mentions the structural reader emits."""
    extraction = parse_extraction(chunk, structural_envelope(chunk))
    return {(m.surface_form, m.entity_type) for m in extraction.mentions}


# The roster table a `### <Team>` section renders (world_builders/markdown.py): a
# people table whose lead row carries the ⭐ marker.
_TEAM_ROSTER = (
    "### Platform / Infrastructure\n\n"
    "_Skills: infrastructure, ci_cd, observability_\n\n"
    "| Name | Title | Seniority | Expertise |\n"
    "| --- | --- | --- | --- |\n"
    "| Ben Cho ⭐ | Platform / Infrastructure Lead | principal | ci_cd |\n"
    "| Cleo Costa | Senior Engineer | senior | ci_cd |\n"
    "| Quinn Greco | Mid Engineer | mid | ci_cd |\n"
)
_TEAM_SECTION = "Engineering > Teams > Platform / Infrastructure"


def test_team_roster_recovers_member_of_with_typed_endpoints() -> None:
    chunk = _chunk(_TEAM_ROSTER, _TEAM_SECTION)
    # Every roster row — lead included — is member_of the team.
    assert ("Ben Cho", "member_of", "Platform / Infrastructure") in _edges(chunk)
    assert ("Cleo Costa", "member_of", "Platform / Infrastructure") in _edges(chunk)
    assert ("Quinn Greco", "member_of", "Platform / Infrastructure") in _edges(chunk)
    # Endpoints are emitted as correctly typed mentions (Person → Team).
    mentions = _typed_mentions(chunk)
    assert ("Ben Cho", "Person") in mentions
    assert ("Platform / Infrastructure", "Team") in mentions


def test_team_roster_recovers_leads_from_star_marker() -> None:
    chunk = _chunk(_TEAM_ROSTER, _TEAM_SECTION)
    edges = _edges(chunk)
    # Only the ⭐-marked row leads the team.
    assert ("Ben Cho", "leads", "Platform / Infrastructure") in edges
    assert ("Cleo Costa", "leads", "Platform / Infrastructure") not in edges
    # The marker is stripped from the lead's surface form (no bare "⭐").
    assert all("⭐" not in m for m, _ in _typed_mentions(chunk))


def test_team_roster_recovers_reports_to_the_lead() -> None:
    chunk = _chunk(_TEAM_ROSTER, _TEAM_SECTION)
    edges = _edges(chunk)
    # Non-lead members report to the lead; the lead does not report to itself.
    assert ("Cleo Costa", "reports_to", "Ben Cho") in edges
    assert ("Quinn Greco", "reports_to", "Ben Cho") in edges
    assert not any(rel == "reports_to" and src == "Ben Cho" for src, rel, _ in edges)


def test_team_roster_recovers_part_of_department() -> None:
    chunk = _chunk(_TEAM_ROSTER, _TEAM_SECTION)
    # The team is part_of its department (the breadcrumb root).
    assert ("Platform / Infrastructure", "part_of", "Engineering") in _edges(chunk)
    assert ("Engineering", "Department") in _typed_mentions(chunk)


def test_lead_only_team_emits_no_reports_to() -> None:
    # A team whose sole member is the lead has no one to report to it.
    text = (
        "### Quality Engineering\n\n"
        "| Name | Title | Seniority | Expertise |\n"
        "| --- | --- | --- | --- |\n"
        "| Tara Ibarra ⭐ | Quality Engineering Lead | principal | test_automation |\n"
    )
    edges = _edges(_chunk(text, "Engineering > Teams > Quality Engineering"))
    assert ("Tara Ibarra", "leads", "Quality Engineering") in edges
    assert ("Tara Ibarra", "member_of", "Quality Engineering") in edges
    assert not any(rel == "reports_to" for _, rel, _ in edges)


# The initiatives-and-projects bullet tree a department page renders: a 📦 project
# with role-tagged member children.
_PROJECT_TREE = (
    "## Initiatives & projects\n\n"
    "- **Engineering Program** (program)\n"
    "    - Build Software (scenario · playbook `build_software`)\n"
    "        - 📦 Build Software Delivery\n"
    "            - lead: Yuki Quintero\n"
    "            - contributor: Ben Cho\n"
    "            - reviewer: Cleo Diaz\n"
)


def test_project_roster_recovers_member_of_for_every_role() -> None:
    chunk = _chunk(_PROJECT_TREE, "Engineering > Initiatives & projects")
    edges = _edges(chunk)
    # lead / contributor / reviewer alike are member_of the project.
    assert ("Yuki Quintero", "member_of", "Build Software Delivery") in edges
    assert ("Ben Cho", "member_of", "Build Software Delivery") in edges
    assert ("Cleo Diaz", "member_of", "Build Software Delivery") in edges
    assert ("Build Software Delivery", "Project") in _typed_mentions(chunk)
    # The program/scenario bullets above the 📦 are not members.
    assert not any("Program" in src for src, _, _ in edges)


def test_department_goals_recover_advances_goal() -> None:
    text = "## Advances goals\n\n- Launch the next-generation product line.\n"
    chunk = _chunk(text, "Engineering > Advances goals")
    assert (
        "Engineering",
        "advances_goal",
        "Launch the next-generation product line.",
    ) in _edges(chunk)
    mentions = _typed_mentions(chunk)
    assert ("Engineering", "Department") in mentions
    assert ("Launch the next-generation product line.", "Goal") in mentions


def test_non_roster_chunk_is_a_noop() -> None:
    # Prose (and any chunk that matches no org-markdown shape) yields nothing.
    envelope = structural_envelope(
        _chunk("Ada Lovelace reports to Grace Hopper.", "Engineering > Platform")
    )
    assert envelope == {"mentions": [], "triples": []}
    # A goals section that is not a *department's* advances-goals list is left to
    # the LLM (esim-ecr.1) — the structural reader does not touch it.
    assert structural_envelope(_chunk("## Goals\n\n- **Grow.**\n", "Company > Goals")) == {
        "mentions": [],
        "triples": [],
    }


def test_missing_section_is_a_noop() -> None:
    assert structural_envelope(_chunk(_TEAM_ROSTER, None)) == {"mentions": [], "triples": []}


# ---------------------------------------------------------------------------
# Integration: extract_chunk merges the structural envelope with the backend's.
# ---------------------------------------------------------------------------


def test_extract_chunk_merges_structural_edges_keylessly() -> None:
    # Even the deterministic fake backend (no key) yields the structural org edges,
    # because extract_chunk merges structural_envelope into every extraction.
    client = build_client(LLMConfig(backend="fake"))
    chunk = _chunk(_TEAM_ROSTER, _TEAM_SECTION)
    triples = {(t.src_mention, t.rel, t.dst_mention) for t in extract_chunk(chunk, client).triples}
    assert ("Ben Cho", "leads", "Platform / Infrastructure") in triples
    assert ("Cleo Costa", "reports_to", "Ben Cho") in triples
    assert ("Platform / Infrastructure", "part_of", "Engineering") in triples


def test_merge_envelopes_unions_and_parse_dedups() -> None:
    # Merge concatenates; a single parse_extraction pass dedups the overlap.
    llm = {
        "mentions": [{"surface_form": "Ben Cho", "type": "Person"}],
        "triples": [{"src": "Ben Cho", "rel": "leads", "dst": "Platform"}],
    }
    structural = {
        "mentions": [{"surface_form": "Platform", "type": "Team"}],
        "triples": [{"src": "Ben Cho", "rel": "leads", "dst": "Platform", "confidence": 1.0}],
    }
    merged = merge_envelopes(llm, structural)
    assert len(merged["triples"]) == 2  # union, before dedup
    chunk = _chunk("Ben Cho leads Platform.", "Engineering > Platform")
    result = parse_extraction(chunk, merged)
    assert len(result.triples) == 1  # the shared (Ben Cho, leads, Platform) collapses


def test_merge_envelopes_tolerates_malformed() -> None:
    merged = merge_envelopes({"mentions": None, "triples": "bad"}, {"mentions": [{"x": 1}]})
    assert merged == {"mentions": [{"x": 1}], "triples": []}
