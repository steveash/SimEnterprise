"""``[org]``: department cap, squads per team shape, turnover and transfers; kit archetypes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, load_templates
from enterprise_sim.core.world import Node
from enterprise_sim.world_builders import build_world

_BASE: dict[str, Any] = {
    "seed": 4,
    "company": {"name": "Org Co", "vertical": "software", "size": "large"},
    "simulation": {"period_start": "2026-01-05", "period_end": "2026-06-26"},
}

_KITS = (
    "data_engineering",
    "data_analytics",
    "security",
    "finance",
    "people_ops",
    "support",
    "sales",
    "marketing",
    "legal",
)


def _world(**sections: Any) -> Any:
    return build_world(WorldConfig.model_validate({**_BASE, **sections}))


def _departments(world: Any) -> list[str]:
    return [str(n.props["archetype"]) for n in world.nodes_by_type("Department")]


def test_size_band_selection_is_unchanged_by_new_archetypes() -> None:
    # large = 2 departments: the vertical's primary, then the legacy built-in.
    assert sorted(_departments(_world())) == ["engineering", "retail"]


def test_max_departments_reaches_further_archetypes_deterministically() -> None:
    first = _departments(_world(org={"max_departments": 6}))
    second = _departments(_world(org={"max_departments": 6}))
    assert first == second and len(first) == 6
    assert {"engineering", "retail"} <= set(first)


def test_a_vertical_claimed_by_a_kit_picks_it_as_primary() -> None:
    world = build_world(
        WorldConfig.model_validate(
            {**_BASE, "company": {"name": "D", "vertical": "analytics", "size": "small"}}
        )
    )
    assert _departments(world) == ["data_analytics"]


def test_squads_multiply_teams_and_keep_one_head() -> None:
    plain = _world()
    squads = _world(org={"squads_per_team": "2..3"})
    assert len(squads.nodes_by_type("Team")) > len(plain.nodes_by_type("Team"))
    for dept in squads.nodes_by_type("Department"):
        assert len(squads.in_edges(dept.id, "leads")) == 1
    names = [str(t.props["name"]) for t in squads.nodes_by_type("Team")]
    assert len(names) == len(set(names))


def _dt(text: object) -> datetime:
    return datetime.fromisoformat(str(text))


def test_turnover_adds_leavers_and_successors() -> None:
    world = _world(org={"turnover_rate": 0.3})
    people = world.nodes_by_type("Person")
    leavers = [p for p in people if "left_at" in p.props]
    joiners = [p for p in people if "joined_at" in p.props]
    assert leavers and joiners
    leads = {e.src for e in world.edges_by_type("leads")}
    assert not leads & {p.id for p in leavers}
    for joiner in joiners:
        (replaced,) = world.out_edges(joiner.id, "replaces")
        leaver = world.get_node(replaced.dst)
        assert leaver is not None
        assert _dt(joiner.props["joined_at"]) > _dt(leaver.props["left_at"])
        assert joiner.created_at == _dt(joiner.props["joined_at"])
        assert world.out_edges(joiner.id, "member_of")
        assert world.out_edges(joiner.id, "reports_to")
    for leaver in leavers:
        assert all("until" in e.props for e in world.out_edges(leaver.id, "member_of"))


def test_transfers_move_people_within_their_department() -> None:
    world = _world(org={"transfer_rate": 0.4, "squads_per_team": "1..2"})
    moved = [p for p in world.nodes_by_type("Person") if "transferred_at" in p.props]
    assert moved
    for person in moved:
        teams = world.out_edges(person.id, "member_of")
        team_edges = [e for e in teams if e.dst.startswith("team:")]
        assert len(team_edges) == 2
        old = [e for e in team_edges if "until" in e.props]
        assert len(old) == 1
        depts = {str(world.get_node(e.dst).props["department"]) for e in team_edges}
        assert depts == {person.props["department"]}


def test_without_org_knobs_nobody_turns_over() -> None:
    world = _world()
    assert not [
        p
        for p in world.nodes_by_type("Person")
        if {"left_at", "joined_at", "transferred_at"} & set(p.props)
    ]


@pytest.mark.parametrize(
    ("props", "employed"),
    [
        ({}, True),
        ({"joined_at": "2026-02-01T09:00:00"}, False),
        ({"joined_at": "2026-01-01T09:00:00"}, True),
        ({"left_at": "2026-01-10T09:00:00"}, False),
        ({"left_at": "2026-01-15T09:00:00"}, True),
        ({"left_at": "2026-01-14T09:00:00"}, False),
    ],
)
def test_the_resolver_only_picks_people_on_staff(props: dict[str, str], employed: bool) -> None:
    from enterprise_sim.core.sim.resolver import _employed

    node = Node("person:x", "Person", datetime(2026, 1, 1, 9), props=props)
    assert _employed(node, datetime(2026, 1, 14, 9)) is employed


@pytest.mark.parametrize("name", _KITS)
def test_every_kit_registers_a_runnable_department(name: str) -> None:
    load_templates()
    spec = ARCHETYPES.get(name)
    assert isinstance(spec, DepartmentArchetypeSpec)
    assert spec.lead_role and spec.playbooks == (f"run_{name}",)
    assert f"run_{name}" in PLAYBOOKS


def test_every_kit_department_produces_authored_documents(tmp_path: Any) -> None:
    from enterprise_sim.assembly import execute_run

    config = WorldConfig.model_validate(
        {
            **_BASE,
            "simulation": {"period_start": "2026-01-05", "period_end": "2026-02-27"},
            # retail too: its playbook selects a sales lead and a demand planner,
            # which the archetype's team_lead_roles provide.
            "departments": [{"archetype": name} for name in (*_KITS, "retail")],
            "worlds_root": str(tmp_path),
        }
    )
    result = execute_run(config)
    world = result.world
    by_dept: dict[str, int] = {}
    authored = 0
    for artifact in world.nodes_by_type("Artifact"):
        path = str(artifact.props.get("path", ""))
        for name in _KITS:
            if f"initiative-{name.replace('_', '-')}-run-" in path:
                by_dept[name] = by_dept.get(name, 0) + 1
        if world.in_edges(artifact.id, "authored"):
            authored += 1
    assert set(by_dept) == set(_KITS), by_dept
    artifacts = len(world.nodes_by_type("Artifact"))
    assert authored == artifacts, (authored, artifacts)


def test_project_names_come_from_the_department_bank() -> None:
    from enterprise_sim.world_builders.builder import _PROJECT_NAMES

    legacy = {str(p.props["name"]) for p in _world().nodes_by_type("Project")}
    assert all(name.endswith("Delivery") for name in legacy)
    named = _world(org={"project_names": True})
    for project in named.nodes_by_type("Project"):
        assert project.props["name"] in _PROJECT_NAMES[str(project.props["department"])]
