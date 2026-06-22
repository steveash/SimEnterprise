"""The canonical Gold-KG JSONL exporter (ARCHITECTURE.md §11.4, D19).

JSONL is the **canonical** on-disk form — streamable, diffable line-by-line (two
runs ``git``-diff cleanly), loadable anywhere. This exporter writes the full
``kg/`` set from a :class:`~enterprise_sim.exporters.bundle.KgBundle`:

* ``nodes`` / ``edges`` / ``events`` — the graph + its temporal journal.
* ``provenance`` / ``mentions`` / ``aliases`` — the three answer keys (§11.3).
* ``schema.json`` — the self-describing JSON Schema for all of the above.
* ``graph.json`` — the convenience node-link form (networkx-loadable).

Every line is emitted with sorted keys and compact separators, so identical
inputs yield byte-identical files (D10).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from enterprise_sim.exporters.bundle import KgBundle
from enterprise_sim.exporters.registry import EXPORTERS
from enterprise_sim.exporters.schema import schema_document

__all__ = ["JsonlExporter", "write_jsonl"]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write ``rows`` to ``path`` as deterministic JSONL (one sorted object per line)."""
    lines = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _write_json(path: Path, document: dict[str, Any]) -> Path:
    """Write a pretty, deterministic JSON document (``schema.json`` / ``graph.json``)."""
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


class JsonlExporter:
    """Writes the canonical ``kg/*.jsonl`` set plus ``schema.json`` and ``graph.json``."""

    name = "jsonl"

    def export(self, bundle: KgBundle, out_dir: Path) -> list[Path]:
        """Write the full canonical KG under ``out_dir``; return the files written."""
        out_dir.mkdir(parents=True, exist_ok=True)
        written = [
            write_jsonl(out_dir / "nodes.jsonl", list(bundle.nodes)),
            write_jsonl(out_dir / "edges.jsonl", list(bundle.edges)),
            write_jsonl(out_dir / "events.jsonl", list(bundle.events)),
            write_jsonl(out_dir / "provenance.jsonl", list(bundle.provenance)),
            write_jsonl(out_dir / "mentions.jsonl", list(bundle.mentions)),
            write_jsonl(out_dir / "aliases.jsonl", list(bundle.aliases)),
            _write_json(out_dir / "schema.json", schema_document()),
            _write_json(out_dir / "graph.json", bundle.graph_json()),
        ]
        return written


#: Registered instance — the canonical writer, looked up as ``EXPORTERS.get("jsonl")``.
JSONL_EXPORTER = EXPORTERS.register(JsonlExporter())
