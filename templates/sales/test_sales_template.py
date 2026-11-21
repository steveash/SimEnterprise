"""The ``sales`` department template registers a runnable archetype + playbook."""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.authoring import lint_playbook
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, load_templates


def test_sales_registers_with_its_playbook() -> None:
    load_templates()
    spec = ARCHETYPES.get("sales")
    assert isinstance(spec, DepartmentArchetypeSpec)
    assert spec.team_shapes and spec.lead_role == "sales_lead"
    assert spec.playbooks == ("run_sales",)
    assert "run_sales" in PLAYBOOKS


def test_sales_playbook_lints_clean() -> None:
    load_templates()
    playbook = PLAYBOOKS.get("run_sales").build()  # type: ignore[attr-defined]
    result = lint_playbook(playbook)
    assert result.ok, [str(d) for d in result.errors()]
