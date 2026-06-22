"""Tests for grounding: roster, mention tagger, unresolved-name detection.

Covers ARCHITECTURE.md §16.2/§11.3 (D30/D20): the constrained roster the producer
feeds the model, the high-precision alias tagger that records mentions, and the
detector that drives the single repair pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

from enterprise_sim.core.world import Node, World
from enterprise_sim.producers.grounding import (
    Roster,
    detect_unresolved_names,
    tag_mentions,
)

_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _person(node_id: str, name: str, *, aliases: list[str] | None = None) -> Node:
    return Node(
        id=node_id, type="Person", created_at=_T0, props={"name": name}, aliases=aliases or []
    )


def _world(*nodes: Node) -> World:
    world = World()
    for node in nodes:
        world.add_node(node)
    return world


def _roster() -> Roster:
    world = _world(
        _person("person:ada", "Ada Lovelace", aliases=["Ada"]),
        _person("person:alan", "Alan Turing"),
        _person("person:grace", "Grace Hopper"),
    )
    return Roster.from_worldview(world)


# -- Roster -----------------------------------------------------------------


def test_roster_lists_people_with_aliases_and_instruction() -> None:
    block = _roster().roster_block()
    assert "Ada Lovelace (aka Ada)" in block
    assert "Alan Turing" in block
    assert "Do not invent or mention anyone not listed above." in block


def test_roster_empty_when_no_named_entities() -> None:
    roster = Roster.from_worldview(World())
    assert roster.is_empty()
    assert "no in-scope entities" in roster.roster_block()


def test_roster_surface_index_is_longest_first() -> None:
    surfaces = [s for s, _ in _roster().surface_index]
    # "Ada Lovelace" (12) must precede the bare alias "Ada" (3) so the longer
    # surface claims its span first at match time.
    assert surfaces.index("Ada Lovelace") < surfaces.index("Ada")


def test_roster_includes_non_person_named_entities() -> None:
    world = _world(
        _person("person:ada", "Ada Lovelace"),
        Node("project:payments", "Project", _T0, props={"name": "Payments Platform"}),
    )
    roster = Roster.from_worldview(world)
    assert "project:payments" in roster.entity_ids
    assert "Payments Platform" in roster.allowed_surfaces()


# -- tag_mentions -----------------------------------------------------------


def test_tag_mentions_longest_match_wins() -> None:
    text = "Ada Lovelace met Alan Turing."
    mentions = tag_mentions(text, _roster(), artifact_path="a.md")
    by_offset = {(m.locator.offset, m.surface_form): m.entity_id for m in mentions}
    assert by_offset[(0, "Ada Lovelace")] == "person:ada"
    assert (0, "Ada") not in by_offset  # the bare alias never overlaps the full name
    assert by_offset[(17, "Alan Turing")] == "person:alan"


def test_tag_mentions_records_every_occurrence_with_locators() -> None:
    text = "Ada Lovelace shipped it.\nAda reviewed the change."
    mentions = tag_mentions(text, _roster(), artifact_path="a.md")
    ada = [m for m in mentions if m.entity_id == "person:ada"]
    assert [m.surface_form for m in ada] == ["Ada Lovelace", "Ada"]
    # The second occurrence is on line 2 and its offset/length address the span.
    second = ada[1]
    assert second.locator.line == 2
    assert text[second.locator.offset : second.locator.offset + second.locator.length] == "Ada"


def test_tag_mentions_respects_word_boundaries() -> None:
    # "Adair" must not match the alias "Ada"; "Alan" alone must not match "Alan Turing".
    text = "Adair is not Ada. Alan left."
    mentions = tag_mentions(text, _roster(), artifact_path="a.md")
    forms = [(m.surface_form, m.locator.offset) for m in mentions]
    assert ("Ada", 13) in forms
    assert all(not (f == "Ada" and o == 0) for f, o in forms)  # 'Adair' not tagged


def test_tag_mentions_is_ordered_and_deterministic() -> None:
    text = "Alan Turing, then Ada Lovelace, then Ada."
    first = tag_mentions(text, _roster(), artifact_path="a.md")
    second = tag_mentions(text, _roster(), artifact_path="a.md")
    offsets = [m.locator.offset for m in first]
    assert offsets == sorted(offsets)
    assert [m.to_dict() for m in first] == [m.to_dict() for m in second]


def test_tag_mentions_empty_roster_or_text() -> None:
    assert tag_mentions("Ada Lovelace", Roster.from_worldview(World()), artifact_path="a.md") == []
    assert tag_mentions("", _roster(), artifact_path="a.md") == []


# -- detect_unresolved_names ------------------------------------------------


def test_detect_flags_out_of_scope_name() -> None:
    roster = Roster.from_worldview(_world(_person("person:ada", "Ada Lovelace")))
    prose = "Ada Lovelace met with Grace Hopper to review the plan."
    assert detect_unresolved_names(prose, roster) == ["Grace Hopper"]


def test_detect_ignores_in_scope_names() -> None:
    prose = "Ada Lovelace and Alan Turing shipped the release."
    assert detect_unresolved_names(prose, _roster()) == []


def test_detect_ignores_sentence_initial_ordinary_words() -> None:
    roster = Roster.from_worldview(_world(_person("person:ada", "Ada Lovelace")))
    prose = "The team shipped it. Ada Lovelace approved. We are done."
    assert detect_unresolved_names(prose, roster) == []


def test_detect_dedupes_preserving_first_appearance() -> None:
    roster = Roster.from_worldview(_world(_person("person:ada", "Ada Lovelace")))
    prose = "Grace Hopper and Grace Hopper again, plus Brendan Eich."
    assert detect_unresolved_names(prose, roster) == ["Grace Hopper", "Brendan Eich"]


def test_detect_ignores_acronyms_alnum_and_lone_capitals() -> None:
    # High precision: only multi-word First-Last name shapes are candidates, so
    # acronyms (API), alphanumerics (Q3), all-caps (MUST), and lone capitalized
    # words (Payments) never trigger a (costly) repair pass.
    roster = Roster.from_worldview(_world(_person("person:ada", "Ada Lovelace")))
    prose = "The API shipped in Q3. We MUST review Payments before launch."
    assert detect_unresolved_names(prose, roster) == []


def test_detect_ignores_titlecase_runs_opening_with_stopword() -> None:
    roster = Roster.from_worldview(_world(_person("person:ada", "Ada Lovelace")))
    assert detect_unresolved_names("Next Quarter we plan the migration.", roster) == []
