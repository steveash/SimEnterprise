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
import importlib.util
import os
import pkgutil
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

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


# The synthetic top-level package name external plugin modules are imported
# under (EXPLORER_TEMPLATES.md §2.1). It need not (and does not) exist as a
# real package on disk — ``importlib.util.spec_from_file_location`` loads each
# file directly and :mod:`sys.modules` is what makes re-discovery idempotent.
_EXTERNAL_PACKAGE = "enterprise_sim_templates"

# Environment variable naming extra, ``os.pathsep``-separated plugin
# directories (EXPLORER.md §4); mirrors ``PATH``-style env vars.
PLUGIN_PATH_ENV_VAR = "ENTERPRISE_SIM_PLUGIN_PATH"


def _load_external_module(module_name: str, file_path: Path) -> bool:
    """Import ``file_path`` under ``module_name`` unless already cached.

    Returns ``True`` iff the module was imported by this call (``False`` when it
    was already present in :mod:`sys.modules`, making repeated discovery of the
    same path a no-op — the registry would otherwise reject the re-registration
    as a duplicate). A failed import never leaves a half-initialized module
    cached, so a subsequent call (e.g. after the author fixes a bug) retries
    from scratch.
    """
    if module_name in sys.modules:
        return False
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load plugin module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return True


def discover_paths(paths: Iterable[str | Path]) -> list[str]:
    """Import every ``<dir>/<slug>/plugin.py`` (and bare ``<dir>/<name>.py``) under ``paths``.

    This is the external counterpart to :func:`discover`: a **template**
    directory holds user-authored plugins outside the package
    (``docs/EXPLORER_TEMPLATES.md`` §2.1), one per ``<slug>/`` subdirectory
    (``<slug>/plugin.py``), plus any bare ``.py`` file dropped directly in the
    directory. Each module is loaded under the synthetic package name
    ``enterprise_sim_templates.<slug>`` via
    :func:`importlib.util.spec_from_file_location` and cached in
    :mod:`sys.modules`, so calling this twice with the same paths is a no-op
    (the registry rejects duplicate names, so re-importing an already-loaded
    module must never happen). Entries — directories or bare files — whose name
    starts with ``_`` are skipped, mirroring :func:`discover`. A ``paths`` entry
    that is not an existing directory is silently skipped (:func:`plugin_paths`
    already filters to existing directories, but this stays defensive for
    direct callers).

    Returns the synthetic module names imported by *this call* (already-cached
    modules are omitted), in a deterministic (sorted-directory-entry) order.
    """
    imported: list[str] = []
    for raw in paths:
        directory = Path(raw)
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("_"):
                continue
            if entry.is_dir():
                plugin_file = entry / "plugin.py"
                if not plugin_file.is_file():
                    continue
                module_name = f"{_EXTERNAL_PACKAGE}.{entry.name}"
                if _load_external_module(module_name, plugin_file):
                    imported.append(module_name)
            elif entry.is_file() and entry.suffix == ".py":
                module_name = f"{_EXTERNAL_PACKAGE}.{entry.stem}"
                if _load_external_module(module_name, entry):
                    imported.append(module_name)
    return imported


def plugin_paths(config_paths: Sequence[str | Path] = ()) -> list[Path]:
    """Resolve the external plugin directories to discover: config + env.

    Combines ``config_paths`` (typically ``RunConfig.plugins``) with
    :data:`PLUGIN_PATH_ENV_VAR` (``os.pathsep``-split, e.g. ``ENTERPRISE_SIM_
    PLUGIN_PATH=/a:/b`` on POSIX), in that order, resolves each entry to an
    absolute path, drops duplicates (by resolved path) and any entry that is
    not an existing directory, and returns the survivors in first-seen order —
    ready to hand to :func:`discover_paths`.
    """
    raw: list[str | Path] = [*config_paths]
    env_value = os.environ.get(PLUGIN_PATH_ENV_VAR, "")
    if env_value:
        raw.extend(part for part in env_value.split(os.pathsep) if part)

    seen: set[Path] = set()
    result: list[Path] = []
    for item in raw:
        try:
            resolved = Path(item).resolve()
        except OSError:  # pragma: no cover - defensive (e.g. an OS-level error)
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            result.append(resolved)
    return result


__all__ = [
    "ARCHETYPES",
    "DEFAULT_PLUGIN_PACKAGES",
    "PLAYBOOKS",
    "PLUGIN_PATH_ENV_VAR",
    "PROCESSES",
    "PRODUCERS",
    "discover",
    "discover_all",
    "discover_paths",
    "plugin_paths",
]
