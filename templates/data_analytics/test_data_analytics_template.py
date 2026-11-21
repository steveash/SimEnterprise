"""The ``data_analytics`` department template registers a runnable archetype + playbook."""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.authoring import lint_playbook
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, load_templates


def test_data_analytics_registers_with_its_playbook() -> None:
    load_templates()
    spec = ARCHETYPES.get("data_analytics")
    assert isinstance(spec, DepartmentArchetypeSpec)
    assert spec.team_shapes and spec.lead_role == "analytics_lead"
    assert spec.playbooks == ("run_data_analytics",)
    assert "run_data_analytics" in PLAYBOOKS


def test_data_analytics_playbook_lints_clean() -> None:
    load_templates()
    playbook = PLAYBOOKS.get("run_data_analytics").build()  # type: ignore[attr-defined]
    result = lint_playbook(playbook)
    assert result.ok, [str(d) for d in result.errors()]
