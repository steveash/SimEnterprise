"""``templates validate``: the six-step validation loop (EXPLORER_TEMPLATES.md §2.2).

Runs, in order: **import** (isolated subprocess), **lint**, **conformance**,
**archetype sanity**, **tests** (``pytest templates/<slug>``), and **dry run**
(:func:`~enterprise_sim.assembly.estimate_run`). Each step is reported as
``{"step", "ok", "detail"}`` regardless of whether an earlier step failed — a
broken template never raises out of :func:`validate`, it just reports the
failing step(s) — except that a failed **import** stops the run outright (there
is nothing safe to inspect for the later steps): the report then holds only the
one step.

The report's top-level shape is ``{"slug", "ok", "steps": [...], "provides":
{...}}``. :func:`validate` also rewrites the template's ``validation`` field
in ``template.json`` (:mod:`enterprise_sim.templates.store`) to the outcome.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.templates.store import (
    Provides,
    TemplateValidation,
    ValidationStatus,
    now_iso,
    prepare_isolated_import_dir,
    read_template,
    write_template,
)

# -- step 1: import (isolated subprocess) ------------------------------------ #


def _run_import_check(templates_dir: Path, slug: str) -> dict[str, Any]:
    """Run :mod:`enterprise_sim.templates._import_check` in a fresh process."""
    proc = subprocess.run(
        [sys.executable, "-m", "enterprise_sim.templates._import_check", str(templates_dir), slug],
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = proc.stdout.strip()
    if not stdout:
        detail = proc.stderr.strip() or f"import check exited {proc.returncode} with no output"
        return {"ok": False, "error": detail}
    import json

    try:
        return dict(json.loads(stdout.splitlines()[-1]))
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "error": f"unparseable import-check output: {stdout!r}"}


def _import_in_process(templates_dir: Path, slug: str) -> None:
    """Re-run the (already-proven-safe) import in *this* process for steps 2-6."""
    from enterprise_sim.core.registry import discover, discover_paths

    # Built-ins first, so a template's playbook-by-name references resolve.
    discover("enterprise_sim.archetypes")
    discover("enterprise_sim.playbooks")
    discover("enterprise_sim.processes")
    isolated = prepare_isolated_import_dir(templates_dir, slug)
    discover_paths([isolated])


# -- step 2: lint -------------------------------------------------------------- #


def _diagnostic_dict(diag: Any) -> dict[str, Any]:
    return {
        "code": diag.code,
        "severity": diag.severity.value,
        "message": diag.message,
        "location": diag.location,
    }


def _lint_step(provides: dict[str, list[str]]) -> dict[str, Any]:
    from enterprise_sim.authoring.lint import lint_playbook, lint_process
    from enterprise_sim.core.registry import PLAYBOOKS, PROCESSES

    diagnostics: list[dict[str, Any]] = []
    ok = True
    for name in provides["playbooks"]:
        playbook_plugin = PLAYBOOKS.get(name)
        result = lint_playbook(playbook_plugin.build())  # type: ignore[attr-defined]
        diagnostics.extend(_diagnostic_dict(d) for d in result.diagnostics)
        ok = ok and result.ok
    # A process is only linted standalone here when it is not already reached
    # via one of this template's own playbooks (avoids double-reporting).
    playbook_processes = set(provides["playbooks"])
    for name in provides["processes"]:
        if name in playbook_processes:
            continue
        process_plugin = PROCESSES.get(name)
        result = lint_process(process_plugin.build())  # type: ignore[attr-defined]
        diagnostics.extend(_diagnostic_dict(d) for d in result.diagnostics)
        ok = ok and result.ok
    return {"step": "lint", "ok": ok, "detail": diagnostics}


# -- step 3: conformance -------------------------------------------------------- #


def _conformance_step(provides: dict[str, list[str]]) -> dict[str, Any]:
    from enterprise_sim.authoring.testkit import check_conformance, check_playbook, run_playbook
    from enterprise_sim.core.registry import PLAYBOOKS

    violations: list[str] = []
    for name in provides["playbooks"]:
        plugin = PLAYBOOKS.get(name)
        playbook = plugin.build()  # type: ignore[attr-defined]
        violations.extend(str(v) for v in check_playbook(playbook))
        try:
            result = run_playbook(playbook, seed=1)
            violations.extend(str(v) for v in check_conformance(result))
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            violations.append(f"{name}: run_playbook raised {type(exc).__name__}: {exc}")
    return {"step": "conformance", "ok": not violations, "detail": violations}


# -- step 4: archetype sanity ---------------------------------------------------- #


def _archetype_sanity_step(provides: dict[str, list[str]], *, lint_ok: bool) -> dict[str, Any]:
    from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS

    if not provides["archetypes"]:
        return {
            "step": "archetype_sanity",
            "ok": lint_ok,
            "detail": ["playbook-only template (no archetype); using the lint result"],
        }

    detail: list[str] = []
    ok = True
    for name in provides["archetypes"]:
        spec = ARCHETYPES.get(name)
        if not isinstance(spec, DepartmentArchetypeSpec):
            ok = False
            detail.append(f"{name}: not a DepartmentArchetypeSpec ({type(spec).__name__})")
            continue
        if not spec.team_shapes:
            ok = False
            detail.append(f"{name}: no team shapes (need at least one)")
        for shape in spec.team_shapes:
            lo_s, sep, hi_s = shape.count.partition("..")
            try:
                lo = int(lo_s)
                hi = int(hi_s) if sep else lo
            except ValueError:
                ok = False
                detail.append(f"{name}/{shape.title}: bad count range {shape.count!r}")
                continue
            if lo < 1 or hi < lo:
                ok = False
                detail.append(f"{name}/{shape.title}: invalid count range {shape.count!r}")
        for playbook_name in spec.playbooks:
            if playbook_name not in PLAYBOOKS:
                ok = False
                detail.append(f"{name}: references unknown playbook {playbook_name!r}")
    return {"step": "archetype_sanity", "ok": ok, "detail": detail}


# -- step 5: tests --------------------------------------------------------------- #


_PYTEST_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error|skipped)")


def _tests_step(templates_dir: Path, slug: str) -> dict[str, Any]:
    target = templates_dir / slug
    test_files = sorted(target.glob("test_*.py"))
    if not test_files:
        return {
            "step": "tests",
            "ok": True,
            "detail": {"passed": 0, "failed": 0, "note": "no tests"},
        }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    counts = {kind: 0 for kind in ("passed", "failed", "error", "skipped")}
    tail_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    summary_line = tail_lines[-1] if tail_lines else ""
    for count, kind in _PYTEST_SUMMARY_RE.findall(summary_line):
        counts[kind] = int(count)
    ok = proc.returncode == 0
    detail = {**counts, "returncode": proc.returncode, "summary": summary_line}
    if not ok:
        detail["output"] = proc.stdout[-4000:]
    return {"step": "tests", "ok": ok, "detail": detail}


# -- step 6: dry run --------------------------------------------------------------- #


def _dry_run_step(templates_dir: Path, provides: dict[str, list[str]]) -> dict[str, Any]:
    if not provides["archetypes"]:
        return {
            "step": "dry_run",
            "ok": True,
            "detail": {"note": "playbook-only template (no archetype); dry run skipped"},
        }

    from datetime import date

    from enterprise_sim.assembly import estimate_run
    from enterprise_sim.core.config import (
        CompanyConfig,
        CompanySize,
        DepartmentConfig,
        RunConfig,
        SimulationConfig,
    )

    # Force Layer A to pick this template's archetype as the (only) department
    # via `RunConfig.departments` (docs/EXPLORER_RUNS.md §3.6), with the
    # templates dir on `plugins` so it is discovered exactly as in production.
    primary = provides["archetypes"][0]
    config = RunConfig(
        company=CompanyConfig(
            name="Template Dry Run", vertical="template-dry-run", size=CompanySize.STARTUP
        ),
        simulation=SimulationConfig(period_start=date(2026, 1, 5), period_end=date(2026, 1, 9)),
        departments=(DepartmentConfig(archetype=primary),),
        plugins=(str(templates_dir),),
    )

    try:
        estimate = estimate_run(config)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"step": "dry_run", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "step": "dry_run",
        "ok": True,
        "detail": {
            "artifacts": estimate.num_artifacts,
            "estimated_cost_usd": estimate.estimated_cost_usd,
            "model": estimate.model,
            "note": "selected this template's archetype via RunConfig.departments",
        },
    }


# -- orchestration --------------------------------------------------------------- #


def _summary(ok: bool, steps: list[dict[str, Any]]) -> str:
    parts = [f"{s['step']} {'ok' if s['ok'] else 'FAILED'}" for s in steps]
    return ("valid: " if ok else "invalid: ") + ", ".join(parts)


def validate(templates_dir: Path, slug: str) -> dict[str, Any]:
    """Run the six-step validation loop for ``<templates_dir>/<slug>``.

    Returns ``{"slug", "ok", "steps": [...], "provides": {...}}`` and rewrites
    the template's ``validation`` field in ``template.json`` to match. Never
    raises for a broken template (import failures, lint errors, a crashing
    playbook, …) — those are reported as a failing step instead.
    """
    steps: list[dict[str, Any]] = []
    provides: dict[str, list[str]] = {"archetypes": [], "playbooks": [], "processes": []}

    import_result = _run_import_check(templates_dir, slug)
    import_ok = bool(import_result.get("ok"))
    if import_ok:
        provides = {k: list(v) for k, v in import_result.get("provides", {}).items()}
        detail: Any = provides
    else:
        detail = import_result.get("error", "import failed")
    steps.append({"step": "import", "ok": import_ok, "detail": detail})

    if import_ok:
        try:
            _import_in_process(templates_dir, slug)
        except Exception as exc:  # noqa: BLE001 - should not happen (step 1 already proved it safe)
            steps[-1] = {
                "step": "import",
                "ok": False,
                "detail": f"in-process re-import failed: {type(exc).__name__}: {exc}",
            }
            import_ok = False

    if not import_ok:
        ok = False
        report = {"slug": slug, "ok": ok, "steps": steps, "provides": provides}
        _write_validation(templates_dir, slug, ok, steps)
        return report

    lint_result = _lint_step(provides)
    steps.append(lint_result)

    steps.append(_conformance_step(provides))
    steps.append(_archetype_sanity_step(provides, lint_ok=bool(lint_result["ok"])))
    steps.append(_tests_step(templates_dir, slug))
    steps.append(_dry_run_step(templates_dir, provides))

    ok = all(bool(s["ok"]) for s in steps)
    report = {"slug": slug, "ok": ok, "steps": steps, "provides": provides}
    _write_validation(templates_dir, slug, ok, steps)
    return report


def _write_validation(
    templates_dir: Path, slug: str, ok: bool, steps: list[dict[str, Any]]
) -> None:
    """Rewrite ``template.json``'s ``validation`` field with this run's outcome."""
    try:
        template = read_template(templates_dir, slug)
    except (FileNotFoundError, OSError, ValueError):
        return
    status = ValidationStatus.VALID if ok else ValidationStatus.INVALID
    provides_steps = next((s for s in steps if s["step"] == "import" and s["ok"]), None)
    provides = (
        Provides(**{k: tuple(v) for k, v in provides_steps["detail"].items()})
        if provides_steps is not None
        else template.provides
    )
    updated = template.model_copy(
        update={
            "provides": provides,
            "updated_at": now_iso(),
            "validation": TemplateValidation(
                status=status, checked_at=now_iso(), summary=_summary(ok, steps)
            ),
        }
    )
    write_template(templates_dir, updated)


__all__ = ["validate"]
