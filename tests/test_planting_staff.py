"""Who may write a planted document: the project's people employed over its time."""

from __future__ import annotations

from datetime import datetime

from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.facts.planting import staff_at

_T0 = datetime(2026, 1, 1, 9, 0)


def _world() -> World:
    world = World()
    world.add_node(Node("project:p", "Project", _T0))
    people = {
        "person:always": {},
        "person:later-hire": {"joined_at": "2026-04-09T09:00:00"},
        "person:leaver": {"left_at": "2026-03-01T00:00:00"},
        "person:day-leaver": {"left_at": "2026-03-25T12:00:00"},
        "person:spanning": {"joined_at": "2026-02-01T09:00:00", "left_at": "2026-05-01T00:00:00"},
    }
    for pid, props in people.items():
        world.add_node(Node(pid, "Person", _T0, props=props))
        world.add_edge(Edge(f"edge:member_of:{pid}->project:p", "member_of", pid, "project:p", _T0))
    return world


def test_only_people_employed_over_the_whole_window_are_staff() -> None:
    day = (datetime(2026, 3, 25, 0, 0), datetime(2026, 3, 25, 23, 59))
    assert staff_at(_world(), "project:p", *day) == ["person:always", "person:spanning"]


def test_a_later_hire_is_staff_once_employed() -> None:
    at = datetime(2026, 4, 10, 10, 0)
    assert "person:later-hire" in staff_at(_world(), "project:p", at, at)
    assert "person:later-hire" not in staff_at(_world(), "project:p", _T0, _T0)
