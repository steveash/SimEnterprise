"""Tests for the department archetype plugins (esim-c1756f17).

Acceptance: the ``engineering`` and ``retail`` archetypes both register into the
shared ``ARCHETYPES`` catalog and carry believable Layer-A biasing metadata, and
the playbooks they declare are real registered/reference playbooks.
"""

from __future__ import annotations

import pytest
from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape
from enterprise_sim.archetypes.engineering import ENGINEERING
from enterprise_sim.archetypes.retail import RETAIL
from enterprise_sim.authoring.patterns import REFERENCE_PLAYBOOKS
from enterprise_sim.core.registry import ARCHETYPES, DepartmentArchetype, discover

ARCHETYPE_SPECS = [ENGINEERING, RETAIL]


# --- discovery + registration ----------------------------------------------


def test_discovery_registers_both_archetypes() -> None:
    # Importing the package's submodules (what discovery does) is what fires the
    # registration side effects; idempotent because Python caches the modules.
    discover("enterprise_sim.archetypes")
    assert "engineering" in ARCHETYPES
    assert "retail" in ARCHETYPES


def test_registered_objects_are_the_module_constants() -> None:
    discover("enterprise_sim.archetypes")
    assert ARCHETYPES.get("engineering") is ENGINEERING
    assert ARCHETYPES.get("retail") is RETAIL


# --- protocol conformance ---------------------------------------------------


@pytest.mark.parametrize("spec", ARCHETYPE_SPECS, ids=lambda s: s.name)
def test_spec_satisfies_department_archetype_protocol(spec: DepartmentArchetypeSpec) -> None:
    # runtime_checkable structural protocol: name + playbooks are enough.
    assert isinstance(spec, DepartmentArchetype)
    assert isinstance(spec.name, str) and spec.name
    assert len(spec.playbooks) >= 1


# --- biasing metadata is believable ----------------------------------------


@pytest.mark.parametrize("spec", ARCHETYPE_SPECS, ids=lambda s: s.name)
def test_spec_has_charter_goals_and_teams(spec: DepartmentArchetypeSpec) -> None:
    assert spec.charter.strip()
    assert spec.typical_goals, "an archetype should declare typical goals"
    assert spec.team_shapes, "an archetype should declare typical team shapes"
    for team in spec.team_shapes:
        assert isinstance(team, TeamShape)
        assert team.title.strip()
        low, _, high = team.count.partition("..")
        assert low.isdigit() and high.isdigit(), f"bad count range: {team.count!r}"
        assert int(low) <= int(high)


@pytest.mark.parametrize("spec", ARCHETYPE_SPECS, ids=lambda s: s.name)
def test_declared_playbooks_are_real(spec: DepartmentArchetypeSpec) -> None:
    # Every playbook an archetype claims to run is a known reference playbook.
    for playbook in spec.playbooks:
        assert playbook in REFERENCE_PLAYBOOKS, f"{spec.name} → unknown playbook {playbook!r}"


# --- the two archetypes are distinct domains --------------------------------


def test_engineering_runs_build_software() -> None:
    assert "build_software" in ENGINEERING.playbooks


def test_retail_runs_sell_merchandise() -> None:
    assert "sell_merchandise" in RETAIL.playbooks
