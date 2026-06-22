"""The ``deliverable.kind → producer`` binding map (ARCHITECTURE.md §4, D4/D5).

This is the seam that makes the producer roadmap purely additive. The event
simulator emits format-agnostic deliverables; this map decides which producer(s)
render each deliverable *kind*, so:

- **v1** binds every kind to the ``markdown`` producer (a ``default``);
- **v2** rebinds ``status_report``/``design_doc`` to a ``word`` producer with the
  simulator untouched;
- bindings may be **one-to-many** (one deliverable → docx + Jira + email) to
  produce a cross-modal, KG-consistent corpus.

Bindings are stored by producer *name* and resolved against a producer
:class:`~enterprise_sim.core.registry.registry.Registry` on demand, so the map is
plain config and carries no format dependency itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from enterprise_sim.core.registry.plugins import Producer
from enterprise_sim.core.registry.registry import Registry, UnknownPluginError


class BindingError(Exception):
    """Raised when a deliverable kind cannot be resolved to any producer."""


class BindingMap:
    """Maps abstract deliverable kinds to the producer(s) that render them.

    A ``default`` producer (v1: ``markdown``) catches any kind without an
    explicit binding, so a deliverable can never go unrendered.
    """

    def __init__(self, *, default: str | None = None) -> None:
        self._bindings: dict[str, list[str]] = {}
        self._default = default

    @property
    def default(self) -> str | None:
        """Producer name used for kinds with no explicit binding."""
        return self._default

    def set_default(self, producer: str) -> None:
        """Set the fallback producer name."""
        self._default = producer

    def bind(self, kind: str, *producers: str) -> None:
        """Bind ``kind`` to one or more producer names (appended, de-duplicated).

        Order is preserved so a one-to-many fan-out renders deterministically.
        """
        if not producers:
            raise ValueError(f"no producers given for deliverable kind {kind!r}")
        bound = self._bindings.setdefault(kind, [])
        for producer in producers:
            if producer not in bound:
                bound.append(producer)

    def producer_names(self, kind: str) -> list[str]:
        """Producer name(s) bound to ``kind`` (falling back to ``default``).

        Raises :class:`BindingError` if the kind is unbound and no default is set.
        """
        if kind in self._bindings:
            return list(self._bindings[kind])
        if self._default is not None:
            return [self._default]
        raise BindingError(f"deliverable kind {kind!r} has no binding and no default producer")

    def resolve(self, kind: str, producers: Registry[Producer]) -> list[Producer]:
        """Resolve ``kind`` to live producer plugins from ``producers``.

        Combines the binding lookup with the registry lookup so callers get the
        actual plugins. Propagates :class:`UnknownPluginError` if a bound name is
        not registered — a binding that points at a missing producer is a config
        error worth surfacing loudly.
        """
        return [producers.get(name) for name in self.producer_names(kind)]

    def bindings(self) -> Mapping[str, list[str]]:
        """A read-only snapshot of the explicit kind → names bindings."""
        return {kind: list(names) for kind, names in self._bindings.items()}

    @classmethod
    def from_config(
        cls,
        bindings: Mapping[str, str | Iterable[str]],
        *,
        default: str | None = None,
    ) -> BindingMap:
        """Build a map from a config mapping of kind → producer name(s)."""
        result = cls(default=default)
        for kind, value in bindings.items():
            names = [value] if isinstance(value, str) else list(value)
            result.bind(kind, *names)
        return result


__all__ = ["BindingError", "BindingMap", "UnknownPluginError"]
