"""The ``engineering`` department template registers and runs its playbook."""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, load_templates


def test_engineering_registers_with_a_registered_playbook() -> None:
    load_templates()
    spec = ARCHETYPES.get("engineering")
    assert isinstance(spec, DepartmentArchetypeSpec)
    assert spec.team_shapes, "a department needs at least one team shape"
    assert spec.playbooks == ("build_software",)
    assert "build_software" in PLAYBOOKS
