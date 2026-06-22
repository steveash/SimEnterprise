"""Layer A world-builder tests (esim-65bf4594).

Acceptance: a **deterministic** world from a seed, a **queryable** KG populated
with the full org hierarchy, and seeded latent affinities the actor resolver can
read. These tests assert structure (company → goals → departments → teams →
people → initiatives → projects), determinism, referential integrity, and that
the generated org is directly selectable by the reference playbooks + resolver.
"""

from __future__ import annotations

import random
from datetime import datetime

import pytest
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.core.sim.resolver import Filter, Resolver, Selector
from enterprise_sim.world_builders import build_world
from enterprise_sim.world_builders.builder import (
    E_ADVANCES_GOAL,
    E_COLLABORATES_WITH,
    E_LEADS,
    E_MEMBER_OF,
    E_OWNS_INITIATIVE,
    E_REPORTS_TO,
    E_SUBGOAL_OF,
    E_SUBINITIATIVE_OF,
    E_UNDER,
    N_COMPANY,
    N_DEPARTMENT,
    N_GOAL,
    N_INITIATIVE,
    N_PERSON,
    N_PROJECT,
    N_TEAM,
)


def _config(
    *,
    seed: int = 7,
    vertical: str = "software",
    size: str = "medium",
    name: str = "Acme Corp",
    projects: list[dict[str, str]] | None = None,
) -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": name, "vertical": vertical, "size": size},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-03-31"},
            "seed": seed,
            "projects": projects or [],
        }
    )


# --- determinism (the headline acceptance) ---------------------------------


def test_same_seed_reproduces_identical_kg() -> None:
    a = build_world(_config(seed=42))
    b = build_world(_config(seed=42))
    assert a.to_json() == b.to_json()


def test_different_seed_changes_the_world() -> None:
    a = build_world(_config(seed=1))
    b = build_world(_config(seed=2))
    # Same shape (size/vertical unchanged) but different staffing/names.
    assert a.to_json() != b.to_json()


def test_genesis_nodes_share_the_window_start_timestamp() -> None:
    world = build_world(_config())
    expected = datetime(2026, 1, 1, 9, 0)
    assert all(node.created_at == expected for node in world.nodes())


# --- the org hierarchy is present ------------------------------------------


def test_company_goals_departments_people_projects_exist() -> None:
    world = build_world(_config(size="medium"))
    assert len(world.nodes_by_type(N_COMPANY)) == 1
    assert world.nodes_by_type(N_GOAL), "expected company goals"
    assert world.nodes_by_type(N_DEPARTMENT), "expected departments"
    assert world.nodes_by_type(N_TEAM), "expected teams"
    assert world.nodes_by_type(N_PERSON), "expected people"
    assert world.nodes_by_type(N_INITIATIVE), "expected initiatives"
    assert world.nodes_by_type(N_PROJECT), "expected projects"


def test_size_scales_headcount() -> None:
    startup = build_world(_config(size="startup"))
    enterprise = build_world(_config(size="enterprise"))
    assert len(enterprise.nodes_by_type(N_PERSON)) > len(startup.nodes_by_type(N_PERSON))


def test_vertical_selects_the_primary_archetype() -> None:
    software = build_world(_config(vertical="software", size="startup"))
    retail = build_world(_config(vertical="retail", size="startup"))
    assert {d.props["archetype"] for d in software.nodes_by_type(N_DEPARTMENT)} == {"engineering"}
    assert {d.props["archetype"] for d in retail.nodes_by_type(N_DEPARTMENT)} == {"retail"}


# --- intent hierarchy: goals nest, scenarios bind playbooks ----------------


def test_goals_can_nest() -> None:
    world = build_world(_config(size="enterprise"))
    subgoals = world.edges_by_type(E_SUBGOAL_OF)
    assert subgoals, "expected at least one nested sub-goal (D14)"
    for edge in subgoals:
        assert world.get_node(edge.src) is not None
        assert world.get_node(edge.dst) is not None


def test_scenarios_bind_a_playbook_and_nest_under_a_program() -> None:
    world = build_world(_config())
    scenarios = [n for n in world.nodes_by_type(N_INITIATIVE) if n.props.get("type") == "scenario"]
    assert scenarios
    for scenario in scenarios:
        assert scenario.props.get("playbook"), "a scenario must bind a playbook"
        parents = world.neighbors(scenario.id, E_SUBINITIATIVE_OF, direction="out")
        assert len(parents) == 1
        assert parents[0].props.get("type") == "program"


def test_projects_hang_off_initiatives_with_roled_members() -> None:
    world = build_world(_config())
    for project in world.nodes_by_type(N_PROJECT):
        initiatives = world.neighbors(project.id, E_UNDER, direction="out")
        assert len(initiatives) == 1
        roles = {e.props.get("role") for e in world.in_edges(project.id, E_MEMBER_OF)}
        assert "lead" in roles, "every project needs a lead"


def test_config_projects_are_anchored() -> None:
    world = build_world(_config(projects=[{"name": "Checkout Revamp", "description": "Rebuild"}]))
    project = world.get_node("project:checkout-revamp")
    assert project is not None
    assert project.props["description"] == "Rebuild"
    assert world.neighbors(project.id, E_UNDER, direction="out"), "anchor needs a parent initiative"


# --- reporting / leadership lines ------------------------------------------


def test_every_person_belongs_to_a_team() -> None:
    world = build_world(_config())
    for person in world.nodes_by_type(N_PERSON):
        teams = world.neighbors(person.id, E_MEMBER_OF, direction="out")
        assert any(t.type == N_TEAM for t in teams), f"{person.id} has no team"


def test_reporting_and_leadership_edges_are_consistent() -> None:
    world = build_world(_config())
    # Every reports_to points person -> person.
    for edge in world.edges_by_type(E_REPORTS_TO):
        assert world.get_node(edge.src).type == N_PERSON  # type: ignore[union-attr]
        assert world.get_node(edge.dst).type == N_PERSON  # type: ignore[union-attr]
    # Each department is led by exactly one person and that lead owns initiatives.
    for dept in world.nodes_by_type(N_DEPARTMENT):
        leads = world.neighbors(dept.id, E_LEADS, direction="in")
        assert len(leads) == 1
        owned = world.neighbors(leads[0].id, E_OWNS_INITIATIVE, direction="out")
        assert owned, "the department head should own its program/scenarios"


# --- referential integrity (queryable, no dangling refs) -------------------


def test_no_dangling_edge_endpoints() -> None:
    world = build_world(_config(size="enterprise", projects=[{"name": "P1"}]))
    ids = {n.id for n in world.nodes()}
    for edge in world.edges():
        assert edge.src in ids, f"dangling src on {edge.id}"
        assert edge.dst in ids, f"dangling dst on {edge.id}"


def test_departments_advance_real_goals() -> None:
    world = build_world(_config())
    goal_ids = {g.id for g in world.nodes_by_type(N_GOAL)}
    advanced = [e for e in world.edges_by_type(E_ADVANCES_GOAL)]
    assert advanced
    for edge in advanced:
        assert edge.dst in goal_ids


# --- latent affinities feed the resolver -----------------------------------


def test_affinities_use_the_resolver_canonical_edge_id() -> None:
    world = build_world(_config())
    affinities = world.edges_by_type(E_COLLABORATES_WITH)
    assert affinities, "Layer A should seed collaborates_with affinities"
    for edge in affinities:
        src, dst = sorted((edge.src, edge.dst))
        assert edge.id == f"edge:{E_COLLABORATES_WITH}:{src}<->{dst}"
        assert edge.props["weight"] >= 1.0


def test_resolver_reads_seeded_affinities() -> None:
    # The build_software reviewer selector must resolve against a Layer-A org,
    # proving the KG is queryable by the reference playbooks + resolver.
    world = build_world(_config(vertical="software", size="medium"))
    resolver = Resolver(world)
    selector = Selector(
        type=N_PERSON,
        where=(Filter("team", "eq", "engineering"),),
        rank_by=("affinity", "inverse_load", "expertise"),
        count="2..3",
    )
    anchor = world.nodes_by_type(N_PERSON)[0].id
    resolution = resolver.resolve(
        selector,
        rng=random.Random(1),
        at=datetime(2026, 2, 1, 9, 0),
        anchor=anchor,
        relationship="reviews_for",
    )
    assert 2 <= len(resolution.ids) <= 3
    assert anchor not in resolution.ids


def test_department_lead_carries_a_playbook_selectable_role() -> None:
    world = build_world(_config(vertical="software", size="startup"))
    leads = [p for p in world.nodes_by_type(N_PERSON) if p.props.get("role") == "eng_lead"]
    assert leads, "the engineering head should be tagged role=eng_lead for build_software"


@pytest.mark.parametrize("vertical", ["software", "retail"])
def test_people_team_prop_matches_archetype(vertical: str) -> None:
    world = build_world(_config(vertical=vertical, size="startup"))
    archetypes = {d.props["archetype"] for d in world.nodes_by_type(N_DEPARTMENT)}
    for person in world.nodes_by_type(N_PERSON):
        assert person.props["team"] in archetypes
