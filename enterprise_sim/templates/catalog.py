"""``templates catalog``: built-ins + templates in one JSON (EXPLORER_TEMPLATES.md §2.2).

What ``job catalog`` embeds and the explorer run form's *Departments*/*Scenario
instances* dropdowns list: every registered archetype/playbook/process,
tagged with where it came from (``"builtin"`` or ``"template:<slug>"``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, PROCESSES, discover, discover_paths
from enterprise_sim.templates.store import (
    list_templates,
    prepare_isolated_import_dir,
    templates_dir,
)


def _snapshot() -> dict[str, set[str]]:
    return {
        "archetypes": set(ARCHETYPES.names()),
        "playbooks": set(PLAYBOOKS.names()),
        "processes": set(PROCESSES.names()),
    }


def build_catalog(root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Built-ins + every *loadable* template under ``root``, as one flat catalog.

    Loads the built-in archetype/playbook/process packages first (attributed
    ``source: "builtin"``), then imports each listed template's plugin **in
    isolation** — a template whose ``plugin.py`` fails to import is silently
    left out of the catalog (mirroring an invalid/unvalidated template not
    being production-ready) rather than being allowed to break every other
    template's listing.
    """
    discover("enterprise_sim.archetypes")
    discover("enterprise_sim.playbooks")
    discover("enterprise_sim.processes")
    builtin = _snapshot()

    root = root if root is not None else templates_dir()
    source_of: dict[str, dict[str, str]] = {"archetypes": {}, "playbooks": {}, "processes": {}}
    for entry in list_templates(root):
        if entry.template is None:
            continue
        before = _snapshot()
        try:
            isolated = prepare_isolated_import_dir(root, entry.slug)
            discover_paths([isolated])
        except Exception:  # noqa: BLE001 - a broken template is just left out
            continue
        after = _snapshot()
        for kind, names in source_of.items():
            for name in after[kind] - before[kind]:
                names.setdefault(name, entry.slug)

    def _source(kind: str, name: str) -> str:
        if name in builtin[kind]:
            return "builtin"
        slug = source_of[kind].get(name)
        return f"template:{slug}" if slug else "unknown"

    archetypes: list[dict[str, Any]] = []
    for name in ARCHETYPES.names():
        spec = ARCHETYPES.get(name)
        charter = spec.charter if isinstance(spec, DepartmentArchetypeSpec) else ""
        archetypes.append(
            {
                "name": name,
                "charter": charter,
                "playbooks": list(spec.playbooks),
                "source": _source("archetypes", name),
            }
        )

    playbooks: list[dict[str, Any]] = []
    for name in PLAYBOOKS.names():
        playbook_plugin = PLAYBOOKS.get(name)
        playbooks.append(
            {
                "name": name,
                "vertical": playbook_plugin.vertical,
                "deliverables": list(playbook_plugin.deliverables),
                "source": _source("playbooks", name),
            }
        )

    processes: list[dict[str, Any]] = []
    for name in PROCESSES.names():
        process_plugin = PROCESSES.get(name)
        processes.append(
            {
                "name": name,
                "emits": list(process_plugin.emits),
                "requests": list(process_plugin.requests),
                "source": _source("processes", name),
            }
        )

    return {"archetypes": archetypes, "playbooks": playbooks, "processes": processes}


__all__ = ["build_catalog"]
