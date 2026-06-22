"""KG interop exporters (ARCHITECTURE.md §11.4-11.5, D19).

JSONL is the canonical on-disk Gold-KG; interop adapters are a small registry
mirroring producers (Neo4j first; rdf/graphml later). Importing this package
registers the built-in exporters into :data:`EXPORTERS` as a side effect, so a
caller can immediately ``EXPORTERS.get("jsonl")`` / ``EXPORTERS.get("neo4j")``.
"""

from __future__ import annotations

from enterprise_sim.exporters.bundle import KgBundle, aliases_records
from enterprise_sim.exporters.jsonl import JsonlExporter, write_jsonl
from enterprise_sim.exporters.neo4j import Neo4jExporter
from enterprise_sim.exporters.registry import EXPORTERS, Exporter, discover_exporters
from enterprise_sim.exporters.schema import (
    KG_SCHEMA,
    SchemaError,
    schema_document,
    validate_rows,
)

__all__ = [
    "EXPORTERS",
    "KG_SCHEMA",
    "Exporter",
    "JsonlExporter",
    "KgBundle",
    "Neo4jExporter",
    "SchemaError",
    "aliases_records",
    "discover_exporters",
    "schema_document",
    "validate_rows",
    "write_jsonl",
]
