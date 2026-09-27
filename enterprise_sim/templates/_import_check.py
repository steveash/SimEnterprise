"""Subprocess entry point for the ``templates validate`` import step (isolation).

Run as ``python -m enterprise_sim.templates._import_check <templates-dir> <slug>``.
Imports the template's ``plugin.py`` in a **fresh process** — so a broken
template can never poison the caller's registries (EXPLORER_TEMPLATES.md §2.2
step 1) — and prints one JSON line to stdout reporting whether the import
succeeded and, on success, which archetype/playbook/process/data-scenario names
the template registered (by each registration's recorded origin module, so the
answer is the same whether or not the other templates were already loaded).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _registries() -> dict[str, Any]:
    from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, PROCESSES

    registries: dict[str, Any] = {
        "archetypes": ARCHETYPES,
        "playbooks": PLAYBOOKS,
        "processes": PROCESSES,
    }
    try:  # the data extra is optional; without it no data scenario can register.
        from enterprise_sim.data_products.scenarios import DATA_SCENARIOS

        registries["data_scenarios"] = DATA_SCENARIOS
    except ModuleNotFoundError:
        pass
    return registries


def provides_of(slug: str) -> dict[str, list[str]]:
    """Every plugin name the template ``slug``'s module registered, by kind."""
    from enterprise_sim.core.registry import TEMPLATE_MODULE_PREFIX

    module = f"{TEMPLATE_MODULE_PREFIX}.{slug}"
    provides: dict[str, list[str]] = {
        "archetypes": [],
        "playbooks": [],
        "processes": [],
        "data_scenarios": [],
    }
    for kind, registry in _registries().items():
        provides[kind] = sorted(n for n in registry.names() if registry.origin(n) == module)
    return provides


def main(argv: list[str]) -> int:
    """Import ``argv[1]``'s template ``argv[0]``/``argv[1]`` and print the JSON result."""
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: <templates-dir> <slug>"}))
        return 2
    templates_dir, slug = Path(argv[0]), argv[1]
    try:
        from enterprise_sim.core.registry import discover_paths
        from enterprise_sim.templates.store import prepare_isolated_import_dir

        isolated = prepare_isolated_import_dir(templates_dir, slug)
        discover_paths([isolated])
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 0
    print(json.dumps({"ok": True, "provides": provides_of(slug)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
