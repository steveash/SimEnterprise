"""The four plugin registries and discovery (ARCHITECTURE.md §4, D2/D4).

The extensibility seam of the simulator: department archetypes, playbooks,
processes, and producers register into shared catalogs, and a config-driven
``deliverable.kind → producer`` binding map routes rendering. The core imports
this package but never a concrete plugin or a format library.
"""

from __future__ import annotations

from enterprise_sim.core.registry.binding import BindingError, BindingMap
from enterprise_sim.core.registry.discovery import (
    ARCHETYPES,
    DEFAULT_PLUGIN_PACKAGES,
    PLAYBOOKS,
    PLUGIN_PATH_ENV_VAR,
    PROCESSES,
    PRODUCERS,
    discover,
    discover_all,
    discover_paths,
    plugin_paths,
)
from enterprise_sim.core.registry.plugins import (
    DepartmentArchetype,
    Playbook,
    Process,
    Producer,
)
from enterprise_sim.core.registry.registry import (
    DuplicateRegistrationError,
    NamedPlugin,
    Registry,
    RegistryError,
    UnknownPluginError,
)

__all__ = [
    # registries
    "ARCHETYPES",
    "PLAYBOOKS",
    "PROCESSES",
    "PRODUCERS",
    "DEFAULT_PLUGIN_PACKAGES",
    "PLUGIN_PATH_ENV_VAR",
    # registry machinery
    "Registry",
    "NamedPlugin",
    "RegistryError",
    "DuplicateRegistrationError",
    "UnknownPluginError",
    # discovery
    "discover",
    "discover_all",
    "discover_paths",
    "plugin_paths",
    # plugin protocols
    "DepartmentArchetype",
    "Playbook",
    "Process",
    "Producer",
    # binding
    "BindingMap",
    "BindingError",
]
