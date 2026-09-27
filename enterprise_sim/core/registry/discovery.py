"""Plugin discovery and the four process-wide default registries.

Every department archetype, playbook, process and data scenario is a
**template**: a ``<templates-dir>/<slug>/plugin.py`` that registers itself into
the shared registries below as an import side effect (docs/EXPLORER_TEMPLATES.md).
The built-in ones are ordinary templates too, shipped in the repo's
``templates/`` directory and bundled into the installed package as
``enterprise_sim/_templates`` — :func:`default_templates_dir` finds whichever
exists, and :func:`plugin_paths` always puts it first. :func:`load_templates` is
the one call that makes every template available.

Producers are the exception: they are engine format backends, not content, and
still live in the ``enterprise_sim.producers`` package (:func:`discover`).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import sys
import warnings
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

#: Maps each plugin kind that still lives in a package to that package. Content
#: plugins (archetypes, playbooks, processes, data scenarios) are templates.
DEFAULT_PLUGIN_PACKAGES: dict[str, str] = {
    "producer": "enterprise_sim.producers",
}

#: The bundled templates' directory name inside the installed package.
BUNDLED_TEMPLATES_DIR = "_templates"


def default_templates_dir() -> Path | None:
    """The built-in templates: bundled with the install, else the repo checkout's ``templates/``.

    An installed wheel carries them at ``enterprise_sim/_templates`` (pyproject's
    ``force-include``); an editable/dev checkout reads ``<repo>/templates``
    directly, which is also where new templates are authored and committed.
    """
    import enterprise_sim

    package = Path(enterprise_sim.__file__).resolve().parent
    for candidate in (package / BUNDLED_TEMPLATES_DIR, package.parent / "templates"):
        if candidate.is_dir():
            return candidate
    return None


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
    """Load every plugin: the package-based kinds (producers) plus every template.

    Returns kind → module names imported by this call, with the template modules
    under ``"templates"``.
    """
    packages = packages if packages is not None else DEFAULT_PLUGIN_PACKAGES
    found = {kind: discover(package) for kind, package in packages.items()}
    found["templates"] = load_templates()
    return found


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


def discover_paths(paths: Iterable[str | Path], *, strict: bool = True) -> list[str]:
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

    With ``strict=False`` a template whose import fails is skipped with a
    :class:`RuntimeWarning` instead of raising, so one half-authored template can
    never stop every other template (or a world build) from loading.
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
                if _load(module_name, plugin_file, strict=strict):
                    imported.append(module_name)
            elif entry.is_file() and entry.suffix == ".py":
                module_name = f"{_EXTERNAL_PACKAGE}.{entry.stem}"
                if _load(module_name, entry, strict=strict):
                    imported.append(module_name)
    return imported


def _load(module_name: str, file_path: Path, *, strict: bool) -> bool:
    if strict:
        return _load_external_module(module_name, file_path)
    try:
        return _load_external_module(module_name, file_path)
    except Exception as exc:  # noqa: BLE001 - a broken template is skipped, not fatal
        warnings.warn(
            f"skipping template {file_path}: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )
        return False


def plugin_paths(config_paths: Sequence[str | Path] = ()) -> list[Path]:
    """Resolve the template directories to discover: built-ins + config + env.

    Starts with :func:`default_templates_dir` (the built-in templates), then
    ``config_paths`` (typically ``WorldConfig.plugins``), then
    :data:`PLUGIN_PATH_ENV_VAR` (``os.pathsep``-split, e.g. ``ENTERPRISE_SIM_
    PLUGIN_PATH=/a:/b`` on POSIX), in that order, resolves each entry to an
    absolute path, drops duplicates (by resolved path) and any entry that is
    not an existing directory, and returns the survivors in first-seen order —
    ready to hand to :func:`discover_paths`.
    """
    default = default_templates_dir()
    raw: list[str | Path] = [*([default] if default is not None else []), *config_paths]
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


def load_templates(config_paths: Sequence[str | Path] = ()) -> list[str]:
    """Make every template available: built-ins + ``config_paths`` + the env var.

    Idempotent (already-imported templates are skipped) and tolerant (a broken
    template is skipped with a warning). This is the call every entry point that
    needs departments/playbooks/processes/data scenarios makes.
    """
    return discover_paths(plugin_paths(config_paths), strict=False)


def ensure_module(module_path: str) -> None:
    """Load the templates first when ``module_path`` names a template module.

    Template modules (``enterprise_sim_templates.<slug>``) only exist once
    discovered; a caller about to resolve one (e.g. a process ``impl`` ref) calls
    this so the lookup doesn't depend on whether discovery already happened.
    """
    if module_path.startswith(f"{_EXTERNAL_PACKAGE}.") and module_path not in sys.modules:
        load_templates()


#: The synthetic package every template module is imported under.
TEMPLATE_MODULE_PREFIX = _EXTERNAL_PACKAGE


__all__ = [
    "ARCHETYPES",
    "BUNDLED_TEMPLATES_DIR",
    "DEFAULT_PLUGIN_PACKAGES",
    "TEMPLATE_MODULE_PREFIX",
    "default_templates_dir",
    "ensure_module",
    "load_templates",
    "PLAYBOOKS",
    "PLUGIN_PATH_ENV_VAR",
    "PROCESSES",
    "PRODUCERS",
    "discover",
    "discover_all",
    "discover_paths",
    "plugin_paths",
]
