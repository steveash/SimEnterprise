"""Plugin discovery and the four process-wide default registries.

Plugins live in dedicated packages (``enterprise_sim.archetypes``,
``…playbooks``, ``…processes``, ``…producers``) and register themselves into the
shared registries below as an import side effect. :func:`discover` imports every
submodule of a package so those side effects fire; :func:`discover_all` does it
for all four kinds. This keeps the core ignorant of concrete plugins — it only
imports the *packages*, never the format libraries inside them.
"""

from __future__ import annotations

import importlib
import pkgutil

from enterprise_sim.core.registry.plugins import (
    DepartmentArchetype,
    Playbook,
    Process,
    Producer,
)
from enterprise_sim.core.registry.registry import Registry

#: Process-wide catalog of department archetypes.
ARCHETYPES: Registry[DepartmentArchetype] = Registry("archetype")
#: Process-wide catalog of playbooks.
PLAYBOOKS: Registry[Playbook] = Registry("playbook")
#: Process-wide catalog of processes.
PROCESSES: Registry[Process] = Registry("process")
#: Process-wide catalog of producers.
PRODUCERS: Registry[Producer] = Registry("producer")

#: Maps each plugin kind to the package whose submodules self-register.
DEFAULT_PLUGIN_PACKAGES: dict[str, str] = {
    "archetype": "enterprise_sim.archetypes",
    "playbook": "enterprise_sim.playbooks",
    "process": "enterprise_sim.processes",
    "producer": "enterprise_sim.producers",
}


def discover(package_name: str) -> list[str]:
    """Import every public submodule of ``package_name``; return their names.

    Submodules whose name starts with ``_`` are skipped. Importing a submodule
    is what triggers its ``@registry.register`` side effects.
    """
    package = importlib.import_module(package_name)
    search_paths = getattr(package, "__path__", None)
    if search_paths is None:
        return []  # not a package (no submodules to walk)
    imported: list[str] = []
    for module_info in pkgutil.iter_modules(search_paths):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"{package_name}.{module_info.name}")
        imported.append(module_info.name)
    return imported


def discover_all(
    packages: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Run :func:`discover` for every plugin kind; return kind → module names."""
    packages = packages if packages is not None else DEFAULT_PLUGIN_PACKAGES
    return {kind: discover(package) for kind, package in packages.items()}


__all__ = [
    "ARCHETYPES",
    "DEFAULT_PLUGIN_PACKAGES",
    "PLAYBOOKS",
    "PROCESSES",
    "PRODUCERS",
    "discover",
    "discover_all",
]
