"""Tests for ``enterprise_sim.templates.{scaffold,validate}``.

The acceptance test (EXPLORER_TEMPLATES.md §5): a freshly scaffolded template
of *every* kind validates green out of the box. A broken template must report
the failing step without raising.

Every test uses a globally-unique slug (see ``tests/test_registry_paths.py``'s
module docstring for why) — the plugin registries and the
``enterprise_sim_templates.*`` module cache are process-wide singletons shared
across the whole test session.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from enterprise_sim.templates.scaffold import scaffold
from enterprise_sim.templates.store import (
    Template,
    TemplateKind,
    ValidationStatus,
    now_iso,
    read_template,
    template_dir,
    write_template,
)
from enterprise_sim.templates.validate import validate


def _slug(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.mark.parametrize(
    "kind", [TemplateKind.DEPARTMENT, TemplateKind.PLAYBOOK, TemplateKind.BUNDLE]
)
def test_scaffold_then_validate_is_green(tmp_path: Path, kind: TemplateKind) -> None:
    slug = _slug(f"ok_{kind.value}")
    scaffold(tmp_path, slug=slug, kind=kind, name=slug.title(), description="a test template")

    report = validate(tmp_path, slug)

    assert report["ok"] is True, report
    assert report["slug"] == slug
    step_names = [s["step"] for s in report["steps"]]
    assert step_names == ["import", "lint", "conformance", "archetype_sanity", "tests", "dry_run"]
    assert all(s["ok"] for s in report["steps"]), report["steps"]


def test_scaffold_then_validate_updates_template_json(tmp_path: Path) -> None:
    slug = _slug("meta")
    scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Meta")

    before = read_template(tmp_path, slug)
    assert before.validation.status is ValidationStatus.UNVALIDATED

    validate(tmp_path, slug)

    after = read_template(tmp_path, slug)
    assert after.validation.status is ValidationStatus.VALID
    assert after.validation.checked_at is not None
    assert after.provides.archetypes == (slug,)


def test_scaffold_department_provides_archetype(tmp_path: Path) -> None:
    slug = _slug("dept")
    scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Dept")
    report = validate(tmp_path, slug)
    assert report["provides"] == {"archetypes": [slug], "playbooks": [], "processes": []}


def test_scaffold_playbook_provides_playbook_and_process(tmp_path: Path) -> None:
    slug = _slug("pb")
    scaffold(tmp_path, slug=slug, kind=TemplateKind.PLAYBOOK, name="PB")
    report = validate(tmp_path, slug)
    assert report["provides"]["archetypes"] == []
    assert report["provides"]["playbooks"] == [f"{slug}_playbook"]
    assert report["provides"]["processes"] == [f"{slug}_kickoff"]


def test_scaffold_bundle_provides_both(tmp_path: Path) -> None:
    slug = _slug("bun")
    scaffold(tmp_path, slug=slug, kind=TemplateKind.BUNDLE, name="Bun")
    report = validate(tmp_path, slug)
    assert report["provides"]["archetypes"] == [slug]
    assert report["provides"]["playbooks"] == [f"{slug}_playbook"]


def test_scaffold_rejects_bad_slug(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scaffold(tmp_path, slug="Not-A-Slug", kind=TemplateKind.DEPARTMENT, name="Nope")


def test_scaffold_rejects_existing_directory(tmp_path: Path) -> None:
    slug = _slug("dup")
    scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Dup")
    with pytest.raises(FileExistsError):
        scaffold(tmp_path, slug=slug, kind=TemplateKind.DEPARTMENT, name="Dup Again")


# -- a broken template reports the failing step, never raises -----------------


def _write_template_json(
    tmp_path: Path, slug: str, kind: TemplateKind = TemplateKind.DEPARTMENT
) -> None:
    stamp = now_iso()
    write_template(
        tmp_path, Template(slug=slug, name=slug, kind=kind, created_at=stamp, updated_at=stamp)
    )


def test_validate_reports_import_failure_without_raising(tmp_path: Path) -> None:
    slug = _slug("broken_import")
    directory = template_dir(tmp_path, slug)
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    _write_template_json(tmp_path, slug)

    report = validate(tmp_path, slug)

    assert report["ok"] is False
    assert report["steps"] == [report["steps"][0]]  # stops after the failing import step
    assert report["steps"][0]["step"] == "import"
    assert report["steps"][0]["ok"] is False
    assert "boom" in report["steps"][0]["detail"]


def test_validate_reports_lint_failure_without_raising(tmp_path: Path) -> None:
    slug = _slug("broken_lint")
    directory = template_dir(tmp_path, slug)
    directory.mkdir(parents=True)
    # A playbook that references an undeclared event -> a lint error, not a crash.
    (directory / "plugin.py").write_text(
        "from __future__ import annotations\n"
        "from collections.abc import Callable, Sequence\n"
        "from dataclasses import dataclass\n"
        "from enterprise_sim.authoring.sdk import (\n"
        "    Activation, Declares, EmittedEvent, OnStart, Playbook, Process,\n"
        "    Role, Selector, Step,\n"
        ")\n"
        "from enterprise_sim.core.registry import PLAYBOOKS, PROCESSES\n\n"
        "def _lead() -> Role:\n"
        "    return Role(name='lead', select=Selector(type='Person', count=1))\n\n"
        "def kickoff() -> Process:\n"
        "    return Process(\n"
        "        name='k', roles=(_lead(),),\n"
        "        steps=(Step(id='s', by='lead', at='day 0',\n"
        "                    emits=(EmittedEvent('NotDeclared'),)),),\n"
        "        declares=Declares(events=()),\n"  # under-declared -> lint error
        "    )\n\n"
        "def pb() -> Playbook:\n"
        "    return Playbook(\n"
        f"        name={slug!r}, vertical='x', roles=(_lead(),),\n"
        "        activations=(Activation(id='a', process=kickoff(), trigger=OnStart()),),\n"
        "    )\n\n"
        "@dataclass(slots=True)\n"
        "class _PP:\n"
        "    name: str\n"
        "    vertical: str\n"
        "    deliverables: Sequence[str]\n"
        "    build: Callable[[], Playbook]\n\n"
        "@dataclass(slots=True)\n"
        "class _PrP:\n"
        "    name: str\n"
        "    emits: Sequence[str]\n"
        "    requests: Sequence[str]\n"
        "    build: Callable[[], Process]\n\n"
        "PROCESSES.register(_PrP('k', ('NotDeclared',), (), kickoff))\n"
        f"PLAYBOOKS.register(_PP({slug!r}, 'x', (), pb))\n",
        encoding="utf-8",
    )
    _write_template_json(tmp_path, slug, kind=TemplateKind.PLAYBOOK)

    report = validate(tmp_path, slug)

    assert report["ok"] is False
    steps_by_name = {s["step"]: s for s in report["steps"]}
    assert steps_by_name["import"]["ok"] is True
    assert steps_by_name["lint"]["ok"] is False
    assert steps_by_name["lint"]["detail"]  # at least one diagnostic
