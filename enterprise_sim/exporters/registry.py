"""The exporter registry (ARCHITECTURE.md §11.5, D19).

Interop exporters are a small registry mirroring producers: the canonical JSONL
writer and the Neo4j adapter register here by name, and future RDF/Turtle and
GraphML adapters slot in behind the same :class:`Exporter` contract without the
core or the run skeleton learning a new format.

The registry machinery itself is the shared :class:`~enterprise_sim.core.registry.Registry`
— a registry holds opaque, name-bearing plugins — so this module only adds the
exporter-shaped protocol and the process-wide :data:`EXPORTERS` catalog.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol, runtime_checkable

from enterprise_sim.core.registry import Registry
from enterprise_sim.exporters.bundle import KgBundle

__all__ = ["EXPORTERS", "Exporter", "discover_exporters"]


@runtime_checkable
class Exporter(Protocol):
    """Writes a :class:`KgBundle` into a target interop format (§11.5).

    Structural contract, mirroring the producer protocol: a stable ``name`` plus
    one method that serializes the canonical bundle under ``out_dir`` and returns
    the files it wrote (run-relative or absolute paths), so the run skeleton can
    record them without knowing the format.
    """

    name: str

    def export(self, bundle: KgBundle, out_dir: Path) -> list[Path]:
        """Write ``bundle`` under ``out_dir``; return the paths written."""
        ...


#: Process-wide catalog of interop exporters (Neo4j first; rdf/graphml later).
EXPORTERS: Registry[Exporter] = Registry("exporter")

# The submodules whose import side effect registers a concrete exporter.
_EXPORTER_MODULES = ("jsonl", "neo4j")


def discover_exporters() -> list[str]:
    """Import every exporter submodule so it self-registers; return their names.

    Idempotent — importlib caches modules, so repeated calls never re-register
    (which the registry would reject as a duplicate).
    """
    for module in _EXPORTER_MODULES:
        importlib.import_module(f"enterprise_sim.exporters.{module}")
    return EXPORTERS.names()
