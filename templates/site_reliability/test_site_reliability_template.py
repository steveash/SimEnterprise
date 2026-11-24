"""The ``sre`` department template registers a runnable archetype + playbook."""

from __future__ import annotations

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.authoring import lint_playbook
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, load_templates


def test_site_reliability_registers_with_its_playbook() -> None:
    load_templates()
    spec = ARCHETYPES.get("site_reliability")
    assert isinstance(spec, DepartmentArchetypeSpec)
    assert spec.team_shapes and spec.lead_role == "site_reliability_lead"
    assert spec.playbooks == ("run_site_reliability",)
    assert "run_site_reliability" in PLAYBOOKS


def test_site_reliability_playbook_lints_clean() -> None:
    load_templates()
    playbook = PLAYBOOKS.get("run_site_reliability").build()  # type: ignore[attr-defined]
    result = lint_playbook(playbook)
    assert result.ok, [str(d) for d in result.errors()]
