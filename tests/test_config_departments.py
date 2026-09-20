"""Config additions: ``[[departments]]`` + project ``playbook``/``department`` (§3.6, §6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from enterprise_sim.assembly import compute_config_digest
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.core.config.models import DepartmentConfig
from enterprise_sim.core.world import Node, World
from enterprise_sim.world_builders import build_world
from enterprise_sim.world_builders.builder import E_UNDER, N_DEPARTMENT, N_INITIATIVE, N_PROJECT
from pydantic import ValidationError


def _config(
    *,
    departments: list[dict[str, str]] | None = None,
    projects: list[dict[str, object]] | None = None,
    vertical: str = "software",
    size: str = "medium",
    seed: int = 7,
) -> RunConfig:
    payload: dict[str, object] = {
        "company": {"name": "Acme Corp", "vertical": vertical, "size": size},
        "simulation": {"period_start": "2026-01-01", "period_end": "2026-03-31"},
        "seed": seed,
    }
    if departments is not None:
        payload["departments"] = departments
    if projects is not None:
        payload["projects"] = projects
    return load_config_from_mapping(payload)


# --- schema: defaults keep old configs valid --------------------------------


def test_run_config_departments_defaults_to_empty() -> None:
    config = _config()
    assert config.departments == ()


def test_project_config_playbook_and_department_default_to_none() -> None:
    config = _config(projects=[{"name": "Anchor"}])
    assert config.projects[0].playbook is None
    assert config.projects[0].department is None


def test_department_config_requires_a_non_empty_archetype() -> None:
    with pytest.raises(ValidationError):
        load_config_from_mapping(
            {
                "company": {"name": "Acme", "vertical": "software", "size": "small"},
                "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
                "departments": [{"archetype": ""}],
            }
        )


# --- _select_archetypes honours explicit departments ------------------------


def test_explicit_departments_are_all_built_overriding_size_driven_limits() -> None:
    # size="startup" would normally cap at 1 department; an explicit list
    # overrides the vertical/size-driven selection entirely, building both.
    config = _config(
        size="startup",
        departments=[{"archetype": "retail"}, {"archetype": "engineering"}],
    )
    world = build_world(config)
    depts = {d.props["archetype"] for d in world.nodes_by_type(N_DEPARTMENT)}
    assert depts == {"retail", "engineering"}


def test_explicit_departments_first_entry_is_the_primary() -> None:
    # "ordered; first = primary" (§3.6) is observed through which department an
    # unnamed project defaults to — `nodes_by_type` itself always returns nodes
    # sorted by id, so it cannot show insertion order directly.
    config = _config(
        departments=[{"archetype": "retail"}, {"archetype": "engineering"}],
        projects=[{"name": "Default Anchor"}],
    )
    world = build_world(config)
    scenario = _scenario_for_project(world, "project:default-anchor")
    assert scenario.props["department"] == "retail"
    assert scenario.props["playbook"] == "sell_merchandise"


def test_explicit_single_department_is_the_primary() -> None:
    config = _config(departments=[{"archetype": "retail"}])
    world = build_world(config)
    assert [d.props["archetype"] for d in world.nodes_by_type(N_DEPARTMENT)] == ["retail"]


def test_unknown_department_archetype_raises_a_clear_error() -> None:
    config = _config(departments=[{"archetype": "nonexistent"}])
    with pytest.raises(ValueError) as excinfo:
        build_world(config)
    message = str(excinfo.value)
    assert "nonexistent" in message
    assert "engineering" in message and "retail" in message  # names the registered ones.


def test_no_explicit_departments_keeps_the_vertical_driven_default() -> None:
    # Byte-for-byte the pre-§3.6 behavior: an omitted `departments` list applies
    # the vertical/size-driven selection unchanged.
    config = _config(vertical="retail", size="small")
    world = build_world(config)
    assert [d.props["archetype"] for d in world.nodes_by_type(N_DEPARTMENT)] == ["retail"]


# --- _build_config_projects: department + playbook attachment --------------


def _scenario_for_project(world: World, project_id: str) -> Node:
    project = world.get_node(project_id)
    assert project is not None
    [initiative] = world.neighbors(project_id, E_UNDER, direction="out")
    return initiative


def test_project_defaults_to_the_primary_department_and_its_first_playbook() -> None:
    config = _config(
        departments=[{"archetype": "engineering"}, {"archetype": "retail"}],
        projects=[{"name": "Default Anchor"}],
    )
    world = build_world(config)
    scenario = _scenario_for_project(world, "project:default-anchor")
    assert scenario.props["department"] == "engineering"  # the primary (first) department.
    assert scenario.props["playbook"] == "build_software"  # engineering's first playbook.


def test_project_attaches_under_its_named_department() -> None:
    config = _config(
        departments=[{"archetype": "engineering"}, {"archetype": "retail"}],
        projects=[{"name": "Retail Anchor", "department": "retail"}],
    )
    world = build_world(config)
    scenario = _scenario_for_project(world, "project:retail-anchor")
    assert scenario.props["department"] == "retail"
    assert scenario.props["playbook"] == "sell_merchandise"  # retail's default playbook.

    dept_node = next(
        d for d in world.nodes_by_type(N_DEPARTMENT) if d.props["archetype"] == "retail"
    )
    program = next(
        n
        for n in world.nodes_by_type(N_INITIATIVE)
        if n.props.get("type") == "program" and n.props.get("department") == "retail"
    )
    assert dept_node is not None and program is not None


def test_project_playbook_is_validated_against_the_registry() -> None:
    config = _config(
        departments=[{"archetype": "engineering"}],
        projects=[{"name": "Bad Anchor", "playbook": "no_such_playbook"}],
    )
    with pytest.raises(ValueError) as excinfo:
        build_world(config)
    message = str(excinfo.value)
    assert "no_such_playbook" in message
    assert "build_software" in message  # names a registered playbook.


def test_project_naming_an_unbuilt_department_raises_a_clear_error() -> None:
    config = _config(
        departments=[{"archetype": "engineering"}],
        projects=[{"name": "Orphan", "department": "retail"}],
    )
    with pytest.raises(ValueError) as excinfo:
        build_world(config)
    message = str(excinfo.value)
    assert "retail" in message
    assert "engineering" in message  # names the departments that WERE built.


def test_explicit_playbook_is_honoured() -> None:
    config = _config(
        departments=[{"archetype": "engineering"}],
        projects=[{"name": "Named Playbook", "playbook": "build_software"}],
    )
    world = build_world(config)
    scenario = _scenario_for_project(world, "project:named-playbook")
    assert scenario.props["playbook"] == "build_software"


def test_multiple_projects_across_departments(tmp_path: Path) -> None:
    config = _config(
        departments=[{"archetype": "engineering"}, {"archetype": "retail"}],
        projects=[
            {"name": "Eng One"},
            {"name": "Retail One", "department": "retail"},
        ],
    )
    world = build_world(config)
    eng_scenario = _scenario_for_project(world, "project:eng-one")
    retail_scenario = _scenario_for_project(world, "project:retail-one")
    assert eng_scenario.props["department"] == "engineering"
    assert retail_scenario.props["department"] == "retail"
    # Every project actually exists as a Project node (no silent drop).
    project_names = {p.props["name"] for p in world.nodes_by_type(N_PROJECT)}
    assert {"Eng One", "Retail One"} <= project_names


# --- digest: the new fields change the run's identity -----------------------


def test_digest_changes_with_departments() -> None:
    base = _config()
    with_departments = _config(departments=[{"archetype": "engineering"}])
    assert compute_config_digest(base) != compute_config_digest(with_departments)


def test_digest_changes_with_project_playbook_and_department() -> None:
    plain = _config(
        departments=[{"archetype": "engineering"}, {"archetype": "retail"}],
        projects=[{"name": "Anchor"}],
    )
    with_playbook = _config(
        departments=[{"archetype": "engineering"}, {"archetype": "retail"}],
        projects=[{"name": "Anchor", "playbook": "build_software"}],
    )
    with_department = _config(
        departments=[{"archetype": "engineering"}, {"archetype": "retail"}],
        projects=[{"name": "Anchor", "department": "retail"}],
    )
    digests = {
        compute_config_digest(plain),
        compute_config_digest(with_playbook),
        compute_config_digest(with_department),
    }
    assert len(digests) == 3  # all three configs are distinct runs.


def test_digest_is_stable_for_an_identical_config() -> None:
    a = _config(departments=[{"archetype": "engineering"}], projects=[{"name": "Anchor"}])
    b = _config(departments=[{"archetype": "engineering"}], projects=[{"name": "Anchor"}])
    assert compute_config_digest(a) == compute_config_digest(b)


def test_departments_field_survives_a_config_round_trip() -> None:
    config = _config(departments=[{"archetype": "retail"}, {"archetype": "engineering"}])
    dumped = config.model_dump(mode="json")
    assert dumped["departments"] == [{"archetype": "retail"}, {"archetype": "engineering"}]
    assert RunConfig.model_validate(dumped) == config
    assert config.departments == (
        DepartmentConfig(archetype="retail"),
        DepartmentConfig(archetype="engineering"),
    )
