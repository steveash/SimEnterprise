"""The ``support`` department template registers a runnable archetype + playbook."""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.authoring import lint_playbook
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, load_templates


def test_support_registers_with_its_playbook() -> None:
    load_templates()
    spec = ARCHETYPES.get("support")
    assert isinstance(spec, DepartmentArchetypeSpec)
    assert spec.team_shapes and spec.lead_role == "support_lead"
    assert spec.playbooks == ("run_support",)
    assert "run_support" in PLAYBOOKS


def test_support_playbook_lints_clean() -> None:
    load_templates()
    playbook = PLAYBOOKS.get("run_support").build()  # type: ignore[attr-defined]
    result = lint_playbook(playbook)
    assert result.ok, [str(d) for d in result.errors()]
