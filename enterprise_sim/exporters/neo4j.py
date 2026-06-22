"""The Neo4j/Cypher interop exporter (ARCHITECTURE.md §11.5, D19).

Neo4j ships first because a property graph is the closest match for the LPG. The
mapping (§11.5):

* node ``type`` → label, ``props`` → properties, ``aliases`` → array property;
* edge ``type`` → a typed relationship carrying its ``props``;
* node-targeted provenance → a real ``(:Artifact)-[:EXPRESSES]->(node)`` edge;
* mentions → ``(:Artifact)-[:MENTIONS {surface_form, …}]->(entity)``.

**Reification wrinkle.** Vanilla Neo4j can't point a relationship at a
relationship, so *edge-targeted* provenance can't be an ``:EXPRESSES`` edge —
it is folded onto the target relationship as an ``expressed_by: [artifact_ids]``
property instead.

Two faithful renderings are emitted: ``import.cypher`` (a ``MERGE`` script for
small graphs) and ``nodes/*.csv`` + ``relationships/*.csv`` (one file per
label / relationship type, for ``neo4j-admin database import``). Both are
deterministic — labels/types and rows are emitted in sorted order.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from enterprise_sim.exporters.bundle import KgBundle
from enterprise_sim.exporters.registry import EXPORTERS

__all__ = ["Neo4jExporter"]

# Reserved relationship types for the two answer keys (§11.5).
_EXPRESSES = "EXPRESSES"
_MENTIONS = "MENTIONS"

_SCALARS = (str, int, float, bool)


def _cypher_literal(value: Any) -> str:
    """Render a Python value as a Cypher literal.

    Scalars and arrays of scalars map directly (JSON is valid Cypher for these);
    a property can't hold a map or a heterogeneous array in Neo4j, so dicts and
    nested arrays are stringified to a JSON string literal instead.
    """
    if value is None or isinstance(value, _SCALARS):
        return json.dumps(value)
    if isinstance(value, list) and all(v is None or isinstance(v, _SCALARS) for v in value):
        return json.dumps(value)
    return json.dumps(json.dumps(value, sort_keys=True))


def _ident(name: str) -> str:
    """Backtick-quote a label or relationship type for safe Cypher embedding."""
    escaped = name.replace("`", "``")
    return f"`{escaped}`"


def _assignments(var: str, props: dict[str, Any]) -> str:
    """Render ``, var.key = literal`` clauses for ``props``, sorted by key."""
    return "".join(
        f", {var}.{key} = {_cypher_literal(value)}" for key, value in sorted(props.items())
    )


def _match(*pairs: tuple[str, str]) -> str:
    """Render a ``MATCH (var {id: …}), …`` prefix for each ``(var, node_id)`` pair."""
    clauses = ", ".join(f"({var} {{id: {_cypher_literal(node_id)}}})" for var, node_id in pairs)
    return f"MATCH {clauses} "


def _scalar_csv(value: Any) -> str:
    """Render a value for a CSV cell: scalars as-is, everything else as JSON text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True)


class Neo4jExporter:
    """Writes ``import.cypher`` plus ``nodes/*.csv`` and ``relationships/*.csv``."""

    name = "neo4j"

    def export(self, bundle: KgBundle, out_dir: Path) -> list[Path]:
        """Write the Neo4j bundle under ``out_dir``; return the files written."""
        out_dir.mkdir(parents=True, exist_ok=True)
        expressed_by = self._edge_targeted_provenance(bundle)

        written: list[Path] = []
        written.append(self._write_cypher(bundle, out_dir, expressed_by))
        written.extend(self._write_node_csvs(bundle, out_dir / "nodes"))
        written.extend(
            self._write_relationship_csvs(bundle, out_dir / "relationships", expressed_by)
        )
        return written

    # -- provenance split ---------------------------------------------------

    def _edge_targeted_provenance(self, bundle: KgBundle) -> dict[str, list[str]]:
        """Map each *edge* id to the artifact node ids whose provenance targets it.

        Node-targeted provenance becomes real ``:EXPRESSES`` edges; edge-targeted
        provenance can't (the reification wrinkle) and is returned here so it can be
        folded onto the relationship as an ``expressed_by`` property.
        """
        edge_ids = {edge["id"] for edge in bundle.edges}
        path_to_artifact = bundle.artifact_path_to_id()
        result: dict[str, list[str]] = {}
        for row in bundle.provenance:
            target = row["target_id"]
            if target not in edge_ids:
                continue
            artifacts = sorted(
                {
                    path_to_artifact[a["path"]]
                    for a in row["artifacts"]
                    if a["path"] in path_to_artifact
                }
            )
            if artifacts:
                result[target] = artifacts
        return result

    def _node_provenance_edges(self, bundle: KgBundle) -> list[tuple[str, str]]:
        """Return ``(artifact_id, node_id)`` pairs for real ``:EXPRESSES`` edges.

        Keeps only node-targeted provenance, and drops an artifact's trivial
        self-provenance (an artifact expressing its own node) to avoid self-loops.
        """
        node_ids = {node["id"] for node in bundle.nodes}
        path_to_artifact = bundle.artifact_path_to_id()
        pairs: set[tuple[str, str]] = set()
        for row in bundle.provenance:
            target = row["target_id"]
            if target not in node_ids:
                continue
            for art in row["artifacts"]:
                artifact_id = path_to_artifact.get(art["path"])
                if artifact_id is None or artifact_id == target:
                    continue
                pairs.add((artifact_id, target))
        return sorted(pairs)

    # -- import.cypher ------------------------------------------------------

    def _write_cypher(
        self,
        bundle: KgBundle,
        out_dir: Path,
        expressed_by: dict[str, list[str]],
    ) -> Path:
        lines = [
            "// Enterprise-Sim Gold KG — Neo4j import (generated; ARCHITECTURE.md §11.5).",
            "// Idempotent: re-running MERGEs the same graph.",
            "",
            "// --- Nodes ---",
        ]
        for node in bundle.nodes:
            props = dict(node["props"])
            props["created_at"] = node["created_at"]
            props["aliases"] = list(node["aliases"])
            lines.append(
                f"MERGE (n:{_ident(node['type'])} {{id: {_cypher_literal(node['id'])}}})"
                f" SET {_assignments('n', props)[2:]};"
            )

        lines.extend(["", "// --- Relationships ---"])
        for edge in bundle.edges:
            props = dict(edge["props"])
            props["created_at"] = edge["created_at"]
            extra = expressed_by.get(edge["id"])
            if extra is not None:
                props["expressed_by"] = extra
            assigns = _assignments("r", props)
            prefix = _match(("a", edge["src"]), ("b", edge["dst"]))
            rel = f"MERGE (a)-[r:{_ident(edge['type'])} {{id: {_cypher_literal(edge['id'])}}}]->(b)"
            lines.append(f"{prefix}{rel}{' SET ' + assigns[2:] if assigns else ''};")

        lines.extend(
            ["", "// --- Provenance (node-targeted): (:Artifact)-[:EXPRESSES]->(node) ---"]
        )
        for artifact_id, target in self._node_provenance_edges(bundle):
            prefix = _match(("a", artifact_id), ("n", target))
            lines.append(f"{prefix}MERGE (a)-[:{_EXPRESSES}]->(n);")

        lines.extend(
            ["", "// --- Mentions: (:Artifact)-[:MENTIONS {surface_form, …}]->(entity) ---"]
        )
        path_to_artifact = bundle.artifact_path_to_id()
        for mention in self._sorted_mentions(bundle):
            mention_artifact = path_to_artifact.get(mention["artifact_path"])
            if mention_artifact is None:
                continue
            loc = mention["locator"]
            props = {
                "surface_form": mention["surface_form"],
                "artifact_path": mention["artifact_path"],
                "medium": loc["medium"],
                "offset": loc["offset"],
                "length": loc["length"],
                "line": loc["line"],
            }
            prefix = _match(("a", mention_artifact), ("e", mention["entity_id"]))
            rel = (
                f"MERGE (a)-[m:{_MENTIONS} "
                f"{{surface_form: {_cypher_literal(mention['surface_form'])}, "
                f"offset: {_cypher_literal(loc['offset'])}}}]->(e)"
            )
            lines.append(f"{prefix}{rel} SET {_assignments('m', props)[2:]};")

        path = out_dir / "import.cypher"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _sorted_mentions(self, bundle: KgBundle) -> list[dict[str, Any]]:
        """Mentions in a stable order (artifact, entity, offset) for byte-stable output."""
        return sorted(
            bundle.mentions,
            key=lambda m: (m["artifact_path"], m["entity_id"], m["locator"]["offset"]),
        )

    # -- CSV (neo4j-admin bulk import) -------------------------------------

    def _write_node_csvs(self, bundle: KgBundle, out_dir: Path) -> list[Path]:
        """One ``nodes/<Label>.csv`` per label: ``:ID,:LABEL,created_at,aliases,props``."""
        out_dir.mkdir(parents=True, exist_ok=True)
        by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in bundle.nodes:
            by_label[node["type"]].append(node)

        written: list[Path] = []
        for label in sorted(by_label):
            rows = [
                [
                    node["id"],
                    label,
                    node["created_at"],
                    ";".join(node["aliases"]),
                    json.dumps(node["props"], sort_keys=True),
                ]
                for node in by_label[label]
            ]
            header = ["id:ID", ":LABEL", "created_at", "aliases:string[]", "props"]
            written.append(_write_csv(out_dir / f"{label}.csv", header, rows))
        return written

    def _write_relationship_csvs(
        self,
        bundle: KgBundle,
        out_dir: Path,
        expressed_by: dict[str, list[str]],
    ) -> list[Path]:
        """One ``relationships/<TYPE>.csv`` per relationship type (typed edges + answer keys)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in bundle.edges:
            by_type[edge["type"]].append(edge)

        written: list[Path] = []
        rel_header = [":START_ID", ":END_ID", ":TYPE", "id", "created_at", "props"]
        for rel_type in sorted(by_type):
            rows = []
            for edge in by_type[rel_type]:
                props = dict(edge["props"])
                extra = expressed_by.get(edge["id"])
                if extra is not None:
                    props["expressed_by"] = extra
                rows.append(
                    [
                        edge["src"],
                        edge["dst"],
                        rel_type,
                        edge["id"],
                        edge["created_at"],
                        json.dumps(props, sort_keys=True),
                    ]
                )
            written.append(_write_csv(out_dir / f"{rel_type}.csv", rel_header, rows))

        # Provenance + mention answer-key relationships.
        prov_rows = [
            [artifact_id, target, _EXPRESSES]
            for artifact_id, target in self._node_provenance_edges(bundle)
        ]
        written.append(
            _write_csv(out_dir / f"{_EXPRESSES}.csv", [":START_ID", ":END_ID", ":TYPE"], prov_rows)
        )

        path_to_artifact = bundle.artifact_path_to_id()
        mention_rows = []
        for mention in self._sorted_mentions(bundle):
            artifact_id = path_to_artifact.get(mention["artifact_path"])
            if artifact_id is None:
                continue
            loc = mention["locator"]
            mention_rows.append(
                [
                    artifact_id,
                    mention["entity_id"],
                    _MENTIONS,
                    mention["surface_form"],
                    loc["medium"],
                    _scalar_csv(loc["offset"]),
                    _scalar_csv(loc["length"]),
                    _scalar_csv(loc["line"]),
                ]
            )
        mention_header = [
            ":START_ID",
            ":END_ID",
            ":TYPE",
            "surface_form",
            "medium",
            "offset:int",
            "length:int",
            "line:int",
        ]
        written.append(_write_csv(out_dir / f"{_MENTIONS}.csv", mention_header, mention_rows))
        return written


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> Path:
    """Write a deterministic CSV (``\\n`` line terminator, QUOTE_MINIMAL)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


#: Registered instance — looked up as ``EXPORTERS.get("neo4j")``.
NEO4J_EXPORTER = EXPORTERS.register(Neo4jExporter())
