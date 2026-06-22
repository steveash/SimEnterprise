"""A small, typed plugin registry.

The registry is the extensibility seam (ARCHITECTURE.md §4, D2/D4): department
archetypes, playbooks, processes, and producers are *registered* into catalogs
rather than wired into the core. The core only ever speaks ``Event`` + KG-entity,
so this module deliberately imports **nothing** domain- or format-specific — a
registry holds opaque, name-bearing plugins keyed by their ``name``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class NamedPlugin(Protocol):
    """The single thing every registrable plugin must expose: a stable name.

    The four plugin protocols (see :mod:`enterprise_sim.core.registry.plugins`)
    extend this. Later milestones add the behavioural methods; registration and
    lookup only ever need the name, which keeps the core decoupled.
    """

    name: str


class RegistryError(Exception):
    """Base class for registry errors."""


class DuplicateRegistrationError(RegistryError):
    """Raised when a plugin name is registered twice in the same registry."""


class UnknownPluginError(RegistryError, KeyError):
    """Raised when a lookup names a plugin that was never registered."""


class Registry[T: NamedPlugin]:
    """An insertion-ordered catalog of named plugins of one kind.

    Registration rejects duplicate names (the duplicate-detection guarantee in
    the acceptance criteria) so two plugins can never silently shadow each other.
    ``register`` returns the plugin unchanged, so it doubles as a decorator::

        @PLAYBOOKS.register
        class BuildSoftware:
            name = "build_software"
    """

    def __init__(self, kind: str) -> None:
        #: human-readable label for this registry, used in error messages.
        self.kind = kind
        self._entries: dict[str, T] = {}

    def register(self, plugin: T) -> T:
        """Add ``plugin`` to the catalog; raise on a duplicate name."""
        name = plugin.name
        if name in self._entries:
            raise DuplicateRegistrationError(f"{self.kind} {name!r} is already registered")
        self._entries[name] = plugin
        return plugin

    def get(self, name: str) -> T:
        """Return the plugin registered under ``name``; raise if unknown."""
        try:
            return self._entries[name]
        except KeyError:
            raise UnknownPluginError(
                f"no {self.kind} named {name!r} (known: {sorted(self._entries)})"
            ) from None

    def names(self) -> list[str]:
        """Registered names, in insertion order."""
        return list(self._entries)

    def items(self) -> Mapping[str, T]:
        """A read-only view of name → plugin (insertion order)."""
        return dict(self._entries)

    def clear(self) -> None:
        """Remove every registration (chiefly for test isolation)."""
        self._entries.clear()

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __iter__(self) -> Iterator[T]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"Registry(kind={self.kind!r}, names={self.names()})"
