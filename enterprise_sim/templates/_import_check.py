"""Subprocess entry point for the ``templates validate`` import step (isolation).

Run as ``python -m enterprise_sim.templates._import_check <templates-dir> <slug>``.
Imports the template's ``plugin.py`` in a **fresh process** — so a broken
template can never poison the caller's registries (EXPLORER_TEMPLATES.md §2.2
step 1) — and prints one JSON line to stdout reporting whether the import
succeeded and, on success, which archetype/playbook/process names it newly
registered (a before/after diff of the four registries).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _snapshot() -> dict[str, list[str]]:
    from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, PROCESSES

    return {
        "archetypes": sorted(ARCHETYPES.names()),
        "playbooks": sorted(PLAYBOOKS.names()),
        "processes": sorted(PROCESSES.names()),
    }


def main(argv: list[str]) -> int:
    """Import ``argv[1]``'s template ``argv[0]``/``argv[1]`` and print the JSON result."""
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: <templates-dir> <slug>"}))
        return 2
    templates_dir, slug = Path(argv[0]), argv[1]

    before = _snapshot()
    try:
        from enterprise_sim.core.registry import discover_paths
        from enterprise_sim.templates.store import prepare_isolated_import_dir

        isolated = prepare_isolated_import_dir(templates_dir, slug)
        discover_paths([isolated])
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 0
    after = _snapshot()
    provides = {kind: sorted(set(after[kind]) - set(before[kind])) for kind in before}
    print(json.dumps({"ok": True, "provides": provides}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
