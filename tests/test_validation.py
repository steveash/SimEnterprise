"""Consistency-validator tests (ARCHITECTURE §11.4, D17).

Acceptance: the validator *detects seeded inconsistencies* — dangling refs,
scheduling conflicts, out-of-window stamps — and a run that trips them still
*completes* (report-and-continue, never hard-fail).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from enterprise_sim.assembly import execute_run, summarize_issue_rows, validate_consistency
from enterprise_sim.assembly.validation import (
    DANGLING_EDGE_ENDPOINT,
    DANGLING_EVENT_REFERENCE,
    OUT_OF_WINDOW,
    SCHEDULING_CONFLICT,
)
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.core.events import Event
from enterprise_sim.core.world import Edge, Node, World

# The window every unit test validates against: all of January 2026.
_WINDOW = (datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 31, 23, 59, 59))
_T0 = datetime(2026, 1, 5, 9, 0)


def _node(node_id: str, *, at: datetime = _T0, type_: str = "Person", **props: object) -> Node:
    return Node(node_id, type_, at, props=dict(props))


def _calendar(node_id: str, person: str, start: datetime, end: datetime) -> Node:
    return Node(
        node_id,
        "CalendarEvent",
        start,
        props={"person": person, "start": start.isoformat(), "end": end.isoformat()},
    )


def _kinds(issues: object) -> set[str]:
    return {issue.kind for issue in issues}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Unit checks.
# --------------------------------------------------------------------------- #


def test_clean_world_has_no_issues() -> None:
    world = World()
    world.add_node(_node("person:a"))
    world.add_node(_node("person:b"))
    world.add_edge(Edge("edge:knows:a-b", "knows", "person:a", "person:b", _T0))
    event = Event("event:1", "Note", _T0, actors={"author": ["person:a"]}, subjects=["person:b"])
    assert validate_consistency(world, [event], window=_WINDOW) == []


def test_detects_dangling_edge_endpoint() -> None:
    world = World()
    world.add_node(_node("person:a"))
    # dst names a node that does not exist.
    world.add_edge(Edge("edge:knows:a-ghost", "knows", "person:a", "person:ghost", _T0))

    issues = validate_consistency(world, [], window=_WINDOW)
    assert _kinds(issues) == {DANGLING_EDGE_ENDPOINT}
    issue = issues[0]
    assert issue.where == "edge:knows:a-ghost"
    assert issue.details["missing"] == "person:ghost"
    assert issue.details["endpoint"] == "dst"


def test_detects_dangling_event_references() -> None:
    world = World()
    world.add_node(_node("person:a"))
    event = Event(
        "event:1",
        "Note",
        _T0,
        actors={"author": ["person:ghost"]},  # unknown actor
        subjects=["person:missing"],  # unknown subject
        parent_event="event:unknown",  # unknown parent
    )

    issues = validate_consistency(world, [event], window=_WINDOW)
    assert _kinds(issues) == {DANGLING_EVENT_REFERENCE}
    missing = {issue.details["missing"] for issue in issues}
    assert missing == {"person:ghost", "person:missing", "event:unknown"}


def test_known_parent_event_is_not_dangling() -> None:
    world = World()
    world.add_node(_node("person:a"))
    parent = Event("event:0", "Note", _T0, actors={"author": ["person:a"]})
    child = Event("event:1", "Reply", _T0, actors={"author": ["person:a"]}, parent_event="event:0")
    assert validate_consistency(world, [parent, child], window=_WINDOW) == []


def test_detects_scheduling_conflict() -> None:
    world = World()
    world.add_node(_node("person:a"))
    # Two bookings for the same person that overlap.
    world.add_node(
        _calendar("cal:1:a", "person:a", datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 5, 10, 0))
    )
    world.add_node(
        _calendar("cal:2:a", "person:a", datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 5, 11, 0))
    )

    issues = validate_consistency(world, [], window=_WINDOW)
    assert _kinds(issues) == {SCHEDULING_CONFLICT}
    assert issues[0].details["person"] == "person:a"
    assert issues[0].details["conflicts_with"] == "cal:1:a"


def test_adjacent_bookings_do_not_conflict() -> None:
    world = World()
    world.add_node(_node("person:a"))
    # Touching at the boundary (half-open intervals) is not an overlap.
    world.add_node(
        _calendar("cal:1:a", "person:a", datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 5, 10, 0))
    )
    world.add_node(
        _calendar("cal:2:a", "person:a", datetime(2026, 1, 5, 10, 0), datetime(2026, 1, 5, 11, 0))
    )
    assert validate_consistency(world, [], window=_WINDOW) == []


def test_same_time_different_people_do_not_conflict() -> None:
    world = World()
    world.add_node(
        _calendar("cal:1:a", "person:a", datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 5, 10, 0))
    )
    world.add_node(
        _calendar("cal:1:b", "person:b", datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 5, 10, 0))
    )
    assert validate_consistency(world, [], window=_WINDOW) == []


def test_detects_out_of_window_node_edge_and_event() -> None:
    world = World()
    before = datetime(2025, 12, 30, 9, 0)
    after = datetime(2026, 2, 2, 9, 0)
    world.add_node(_node("person:a"))
    world.add_node(_node("person:old", at=before))
    world.add_edge(Edge("edge:e", "knows", "person:a", "person:old", after))
    event = Event("event:future", "Note", after, actors={"author": ["person:a"]})

    issues = validate_consistency(world, [event], window=_WINDOW)
    assert _kinds(issues) == {OUT_OF_WINDOW}
    elements = {issue.details["element"] for issue in issues}
    assert elements == {"node", "edge", "event"}


def test_window_boundaries_are_inclusive() -> None:
    world = World()
    world.add_node(_node("person:start", at=datetime(2026, 1, 1, 0, 0)))
    world.add_node(_node("person:end", at=datetime(2026, 1, 31, 23, 59, 59)))
    assert validate_consistency(world, [], window=_WINDOW) == []


def test_summarize_issue_rows_tallies_by_kind() -> None:
    rows: list[dict[str, object]] = [
        {"kind": "out_of_window"},
        {"kind": "dangling_edge_endpoint"},
        {"kind": "out_of_window"},
    ]
    summary = summarize_issue_rows(rows)
    assert summary["total"] == 3
    assert summary["by_kind"] == {"dangling_edge_endpoint": 1, "out_of_window": 2}


# --------------------------------------------------------------------------- #
# Wiring: a run that trips checks still completes.
# --------------------------------------------------------------------------- #


def _config(output_dir: Path) -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": 7,
            "output_dir": str(output_dir),
        }
    )


def test_clean_run_emits_no_consistency_issues(tmp_path: Path) -> None:
    # A real run must not trip the consistency checks (no false positives).
    result = execute_run(_config(tmp_path))
    window = (datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 31, 23, 59, 59, 999999))
    issues = validate_consistency(result.world, result.corpus.journal, window=window)
    assert issues == []


def test_run_summarizes_issues_in_the_manifest(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path))
    validation = result.manifest.validation
    lines = (result.run_dir / "validation" / "issues.jsonl").read_text().splitlines()
    # The manifest summary is an exact tally of what landed on disk.
    assert validation["total"] == len(lines)
    assert sum(validation["by_kind"].values()) == len(lines)


def test_run_completes_despite_seeded_inconsistencies(tmp_path: Path) -> None:
    # report-and-continue (D17): even a deliberately corrupt graph is validated
    # without raising — the validator turns problems into rows, never exceptions.
    world = World()
    world.add_edge(Edge("edge:dangling", "knows", "person:ghost", "person:void", _T0))
    world.add_node(_node("person:old", at=datetime(2020, 1, 1, 9, 0)))
    world.add_node(
        _calendar("cal:1:a", "person:a", datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 5, 12, 0))
    )
    world.add_node(
        _calendar("cal:2:a", "person:a", datetime(2026, 1, 5, 10, 0), datetime(2026, 1, 5, 11, 0))
    )

    issues = validate_consistency(world, [], window=_WINDOW)
    assert _kinds(issues) == {DANGLING_EDGE_ENDPOINT, OUT_OF_WINDOW, SCHEDULING_CONFLICT}
