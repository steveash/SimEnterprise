"""A run whose org uses a *template* archetype, wired via ``plugins``/the env var.

``RunConfig.departments`` (``[[departments]] archetype = <name>``,
EXPLORER_TEMPLATES.md §2.2) is being added by a sibling agent and is not yet
available in this branch, so these tests cannot force-select a template
archetype directly. Instead they lean on ``world_builders.builder.
_select_archetypes``'s existing fallback: when the company's ``vertical``
matches none of ``_VERTICAL_ALIASES``, the *first registered archetype in
sorted-name order* becomes the primary department — so a template slug that
sorts before the built-ins ("engineering", "retail") gets selected
deterministically. Once ``departments`` lands, these tests should be updated to
select the template archetype explicitly instead of relying on sort order.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from enterprise_sim.core.config import CompanyConfig, CompanySize, RunConfig, SimulationConfig
from enterprise_sim.core.registry import ARCHETYPES
from enterprise_sim.templates.scaffold import scaffold
from enterprise_sim.templates.store import TemplateKind
from enterprise_sim.world_builders.builder import build_world


@pytest.fixture(autouse=True)
def _restore_archetypes() -> Iterator[None]:
    """Undo any archetype this module's tests register (ARCHETYPES is a process-wide
    singleton), so ``sorted(registered)[0]`` fallback-selection stays reproducible
    test to test within this file regardless of run order.

    Discovers the built-ins *before* snapshotting so ``original`` always
    includes them — otherwise, if this is the first test in the session to
    trigger ``build_world`` (which discovers them as a side effect), the
    teardown below would wipe out the built-ins it just legitimately added.
    """
    from enterprise_sim.core.registry import discover

    discover("enterprise_sim.archetypes")
    original = dict(ARCHETYPES.items())
    yield
    ARCHETYPES.clear()
    for plugin in original.values():
        ARCHETYPES.register(plugin)


def _slug() -> str:
    # "a"-prefixed so it sorts before the built-in "engineering"/"retail" archetypes.
    return f"aaa_tpl_{uuid.uuid4().hex[:10]}"


def _config(plugins: tuple[str, ...] = ()) -> RunConfig:
    return RunConfig(
        company=CompanyConfig(
            name="Template Co", vertical="unmatched-vertical", size=CompanySize.STARTUP
        ),
        simulation=SimulationConfig(period_start=date(2026, 1, 5), period_end=date(2026, 1, 9)),
        plugins=plugins,
    )


def test_build_world_selects_a_template_archetype_via_plugins_field(tmp_path: Path) -> None:
    slug = _slug()
    scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Template Dept")

    world = build_world(_config(plugins=(str(tmp_path),)))

    depts = list(world.nodes_by_type("Department"))
    assert any(d.props.get("archetype") == slug for d in depts)


def test_build_world_selects_a_template_archetype_via_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = _slug()
    scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Template Dept")
    monkeypatch.setenv("ENTERPRISE_SIM_PLUGIN_PATH", str(tmp_path))

    world = build_world(_config())

    depts = list(world.nodes_by_type("Department"))
    assert any(d.props.get("archetype") == slug for d in depts)


def test_build_world_without_plugins_never_sees_the_template(tmp_path: Path) -> None:
    slug = _slug()
    scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Template Dept")
    assert "ENTERPRISE_SIM_PLUGIN_PATH" not in os.environ

    world = build_world(_config())  # no plugins configured

    depts = list(world.nodes_by_type("Department"))
    assert not any(d.props.get("archetype") == slug for d in depts)
