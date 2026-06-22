"""Gold-KG exporter tests: registry, canonical JSONL, schema, Neo4j (M7, §11.4-11.5)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from enterprise_sim.assembly import execute_run
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.core.world import World
from enterprise_sim.exporters import (
    EXPORTERS,
    KG_SCHEMA,
    KgBundle,
    SchemaError,
    discover_exporters,
    schema_document,
    validate_rows,
)
from enterprise_sim.exporters.bundle import aliases_records


def _config(output_dir: Path) -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": 7,
            "output_dir": str(output_dir),
        }
    )


def _synthetic_bundle() -> KgBundle:
    """A tiny hand-built bundle that exercises every provenance shape.

    One artifact expresses a node (real ``:EXPRESSES`` edge) *and* an edge (the
    reification wrinkle → ``expressed_by`` property), plus a trivial
    self-provenance row that must be dropped, and one mention.
    """
    art_path = "artifacts/s/report.md"
    nodes = [
        {
            "id": "art:report",
            "type": "Artifact",
            "created_at": "2026-01-02T09:00:00",
            "props": {"path": art_path, "title": "Report"},
            "aliases": ["Report"],
        },
        {
            "id": "person:ada",
            "type": "Person",
            "created_at": "2026-01-01T09:00:00",
            "props": {"name": "Ada Lovelace"},
            "aliases": ["Ada Lovelace", "Ada"],
        },
        {
            "id": "proj:x",
            "type": "Project",
            "created_at": "2026-01-01T09:00:00",
            "props": {"name": "Project X", "nested": {"k": 1}},
            "aliases": ["Project X"],
        },
    ]
    edges = [
        {
            "id": "edge:leads:ada:x",
            "type": "leads",
            "src": "person:ada",
            "dst": "proj:x",
            "created_at": "2026-01-01T09:00:00",
            "props": {"since": "2026-01-01"},
        }
    ]
    provenance = [
        {"target_id": "art:report", "artifacts": [{"path": art_path}]},  # self → dropped
        {"target_id": "proj:x", "artifacts": [{"path": art_path}]},  # node → EXPRESSES
        {"target_id": "edge:leads:ada:x", "artifacts": [{"path": art_path}]},  # edge → expressed_by
    ]
    mentions = [
        {
            "artifact_path": art_path,
            "entity_id": "person:ada",
            "surface_form": "Ada Lovelace",
            "locator": {"medium": "markdown", "offset": 3, "length": 12, "line": 1},
        }
    ]
    return KgBundle(
        nodes=tuple(nodes),
        edges=tuple(edges),
        events=(),
        provenance=tuple(provenance),
        mentions=tuple(mentions),
        aliases=tuple(aliases_records(nodes)),
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_builtin_exporters_are_registered() -> None:
    assert "jsonl" in EXPORTERS
    assert "neo4j" in EXPORTERS
    assert EXPORTERS.get("jsonl").name == "jsonl"
    assert EXPORTERS.get("neo4j").name == "neo4j"


def test_discover_exporters_is_idempotent() -> None:
    first = discover_exporters()
    second = discover_exporters()  # must not re-register (would raise on duplicate)
    assert set(first) == set(second) >= {"jsonl", "neo4j"}


# --------------------------------------------------------------------------- #
# Canonical JSONL exporter
# --------------------------------------------------------------------------- #


def test_jsonl_exporter_writes_full_canonical_set(tmp_path: Path) -> None:
    EXPORTERS.get("jsonl").export(_synthetic_bundle(), tmp_path)
    for name in KG_SCHEMA:  # every declared jsonl file
        assert (tmp_path / name).is_file()
    assert (tmp_path / "schema.json").is_file()
    assert (tmp_path / "graph.json").is_file()


def test_canonical_jsonl_is_schema_valid(tmp_path: Path) -> None:
    EXPORTERS.get("jsonl").export(_synthetic_bundle(), tmp_path)
    for name in KG_SCHEMA:
        rows = [json.loads(line) for line in (tmp_path / name).read_text().splitlines()]
        validate_rows(name, rows)  # raises SchemaError on any violation


def test_schema_document_round_trips_as_json() -> None:
    doc = schema_document()
    assert doc["$schema"].startswith("https://json-schema.org/")
    assert set(doc["files"]) == set(KG_SCHEMA)
    json.loads(json.dumps(doc))  # serializable


def test_schema_validator_rejects_a_bad_row() -> None:
    with pytest.raises(SchemaError):
        validate_rows("nodes.jsonl", [{"id": "x", "type": "T"}])  # missing required keys
    with pytest.raises(SchemaError):
        validate_rows(
            "edges.jsonl",
            [{"id": 1, "type": "t", "src": "a", "dst": "b", "created_at": "t", "props": {}}],
        )  # id wrong type


def test_aliases_records_canonical_first() -> None:
    rows = aliases_records(list(_synthetic_bundle().nodes))
    ada = next(r for r in rows if r["entity_id"] == "person:ada")
    assert ada["canonical"] == "Ada Lovelace"
    assert ada["aliases"] == ["Ada"]


def test_graph_json_is_node_link_form(tmp_path: Path) -> None:
    EXPORTERS.get("jsonl").export(_synthetic_bundle(), tmp_path)
    graph = json.loads((tmp_path / "graph.json").read_text())
    assert graph["directed"] is True and graph["multigraph"] is True
    assert len(graph["nodes"]) == 3
    link = graph["links"][0]
    assert link["source"] == "person:ada" and link["target"] == "proj:x"
    assert link["key"] == "edge:leads:ada:x"


# --------------------------------------------------------------------------- #
# Round-trip sanity check (M7 acceptance)
# --------------------------------------------------------------------------- #


def test_jsonl_round_trips_through_the_world(tmp_path: Path) -> None:
    bundle = _synthetic_bundle()
    EXPORTERS.get("jsonl").export(bundle, tmp_path)
    nodes = [json.loads(line) for line in (tmp_path / "nodes.jsonl").read_text().splitlines()]
    edges = [json.loads(line) for line in (tmp_path / "edges.jsonl").read_text().splitlines()]
    rebuilt = World.from_dict({"nodes": nodes, "edges": edges}).to_dict()
    assert rebuilt["nodes"] == nodes
    assert rebuilt["edges"] == edges


# --------------------------------------------------------------------------- #
# Neo4j exporter
# --------------------------------------------------------------------------- #


def test_neo4j_exporter_layout(tmp_path: Path) -> None:
    EXPORTERS.get("neo4j").export(_synthetic_bundle(), tmp_path)
    assert (tmp_path / "import.cypher").is_file()
    # one nodes CSV per label, one relationships CSV per type + the two answer keys.
    labels = {p.stem for p in (tmp_path / "nodes").glob("*.csv")}
    assert labels == {"Artifact", "Person", "Project"}
    rels = {p.stem for p in (tmp_path / "relationships").glob("*.csv")}
    assert {"leads", "EXPRESSES", "MENTIONS"} <= rels


def test_neo4j_cypher_maps_nodes_and_relationships(tmp_path: Path) -> None:
    EXPORTERS.get("neo4j").export(_synthetic_bundle(), tmp_path)
    cypher = (tmp_path / "import.cypher").read_text()
    # node label + array property + scalar property
    assert 'MERGE (n:`Person` {id: "person:ada"})' in cypher
    assert 'n.aliases = ["Ada Lovelace", "Ada"]' in cypher
    # typed relationship carrying its props
    assert 'MERGE (a)-[r:`leads` {id: "edge:leads:ada:x"}]->(b)' in cypher
    # node-targeted provenance → a real :EXPRESSES edge (and NOT a self-loop)
    assert "MERGE (a)-[:EXPRESSES]->(n);" in cypher
    assert '(n {id: "proj:x"}) MERGE (a)-[:EXPRESSES]' in cypher
    assert '(n {id: "art:report"}) MERGE (a)-[:EXPRESSES]' not in cypher  # self dropped
    # mention relationship
    assert "-[m:MENTIONS" in cypher


def test_neo4j_edge_targeted_provenance_becomes_expressed_by(tmp_path: Path) -> None:
    # The reification wrinkle: edge-targeted provenance cannot be an :EXPRESSES edge,
    # so it is folded onto the relationship as an expressed_by property (§11.5).
    EXPORTERS.get("neo4j").export(_synthetic_bundle(), tmp_path)
    cypher = (tmp_path / "import.cypher").read_text()
    assert 'r.expressed_by = ["art:report"]' in cypher
    rel_csv = (tmp_path / "relationships" / "leads.csv").read_text()
    rows = list(csv.DictReader(rel_csv.splitlines()))
    assert json.loads(rows[0]["props"])["expressed_by"] == ["art:report"]


def test_neo4j_node_csv_covers_every_node(tmp_path: Path) -> None:
    bundle = _synthetic_bundle()
    EXPORTERS.get("neo4j").export(bundle, tmp_path)
    seen: set[str] = set()
    for csv_path in (tmp_path / "nodes").glob("*.csv"):
        rows = list(csv.DictReader(csv_path.read_text().splitlines()))
        seen.update(row["id:ID"] for row in rows)
    assert seen == {n["id"] for n in bundle.nodes}


# --------------------------------------------------------------------------- #
# End-to-end wiring
# --------------------------------------------------------------------------- #


def test_run_emits_the_full_kg_export(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path))
    kg = result.run_dir / "kg"
    for name in (*KG_SCHEMA, "schema.json", "graph.json"):
        assert (kg / name).is_file()
    assert (kg / "neo4j" / "import.cypher").is_file()
    assert list((kg / "neo4j" / "nodes").glob("*.csv"))
    assert list((kg / "neo4j" / "relationships").glob("*.csv"))


def test_run_kg_is_schema_valid_and_round_trips(tmp_path: Path) -> None:
    result = execute_run(_config(tmp_path))
    kg = result.run_dir / "kg"
    for name in KG_SCHEMA:
        rows = [json.loads(line) for line in (kg / name).read_text().splitlines()]
        validate_rows(name, rows)
    nodes = [json.loads(line) for line in (kg / "nodes.jsonl").read_text().splitlines()]
    edges = [json.loads(line) for line in (kg / "edges.jsonl").read_text().splitlines()]
    rebuilt = World.from_dict({"nodes": nodes, "edges": edges}).to_dict()
    assert rebuilt["nodes"] == nodes and rebuilt["edges"] == edges


def test_run_kg_export_is_byte_deterministic(tmp_path: Path) -> None:
    stamp = "2026-01-01T00:00:00Z"
    first = execute_run(_config(tmp_path / "a"), generated_at=stamp).run_dir / "kg"
    second = execute_run(_config(tmp_path / "b"), generated_at=stamp).run_dir / "kg"
    for path in sorted(p for p in first.rglob("*") if p.is_file()):
        twin = second / path.relative_to(first)
        assert twin.read_bytes() == path.read_bytes(), path
