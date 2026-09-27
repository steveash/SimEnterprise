"""``templates catalog``: every template's plugins in one JSON (EXPLORER_TEMPLATES.md §2.2).

What ``world catalog`` embeds and the explorer's world form lists: every
registered archetype / playbook / process / data scenario, tagged with the
template that provides it (``"template:<slug>"``). The built-in ones are
ordinary templates too (``templates/`` in the repo, bundled into the install),
so there is no separate "builtin" tier — ``"builtin"`` only remains for a plugin
registered from inside the ``enterprise_sim`` package itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec
from enterprise_sim.core.registry import (
    ARCHETYPES,
    PLAYBOOKS,
    PROCESSES,
    TEMPLATE_MODULE_PREFIX,
    Registry,
    discover_paths,
    load_templates,
)
from enterprise_sim.templates.store import (
    list_templates,
    prepare_isolated_import_dir,
    templates_dir,
)


def _source(registry: Registry[Any], name: str) -> str:
    origin = registry.origin(name) or ""
    if origin.startswith(f"{TEMPLATE_MODULE_PREFIX}."):
        return f"template:{origin[len(TEMPLATE_MODULE_PREFIX) + 1 :]}"
    return "builtin" if origin.startswith("enterprise_sim") else "unknown"


def build_catalog(root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every loadable template (the built-ins + those under ``root``) as one flat catalog.

    Each template under ``root`` is imported **in isolation**, so one whose
    ``plugin.py`` fails to import is simply left out (mirroring an invalid template
    not being production-ready) rather than breaking every other template's listing.
    """
    load_templates()
    root = root if root is not None else templates_dir()
    for entry in list_templates(root):
        if entry.template is None:
            continue
        try:
            discover_paths([prepare_isolated_import_dir(root, entry.slug)])
        except Exception:  # noqa: BLE001 - a broken template is just left out
            continue

    archetypes: list[dict[str, Any]] = []
    for name in ARCHETYPES.names():
        spec = ARCHETYPES.get(name)
        charter = spec.charter if isinstance(spec, DepartmentArchetypeSpec) else ""
        archetypes.append(
            {
                "name": name,
                "charter": charter,
                "playbooks": list(spec.playbooks),
                "source": _source(ARCHETYPES, name),
            }
        )

    playbooks = [
        {
            "name": name,
            "vertical": PLAYBOOKS.get(name).vertical,
            "deliverables": list(PLAYBOOKS.get(name).deliverables),
            "source": _source(PLAYBOOKS, name),
        }
        for name in PLAYBOOKS.names()
    ]

    processes = [
        {
            "name": name,
            "emits": list(PROCESSES.get(name).emits),
            "requests": list(PROCESSES.get(name).requests),
            "source": _source(PROCESSES, name),
        }
        for name in PROCESSES.names()
    ]

    data_scenarios: list[dict[str, Any]] = []
    scenarios: Registry[Any] | None
    try:  # the data extra is optional
        from enterprise_sim.data_products.scenarios import DATA_SCENARIOS as scenarios
    except ModuleNotFoundError:
        scenarios = None
    if scenarios is not None:
        DATA_SCENARIOS = scenarios  # noqa: N806
        for name in sorted(DATA_SCENARIOS.names()):
            plugin = DATA_SCENARIOS.get(name)
            data_scenarios.append(
                {
                    "name": name,
                    "title": plugin.title,
                    "brief": plugin.brief,
                    "source": _source(DATA_SCENARIOS, name),
                }
            )

    return {
        "archetypes": archetypes,
        "playbooks": playbooks,
        "processes": processes,
        "data_scenarios": data_scenarios,
    }


__all__ = ["build_catalog"]
