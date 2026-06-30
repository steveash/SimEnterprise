"""Embedded Cypher (Kùzu) and SPARQL (Oxigraph) engines over the gold KG (esim-uzc.4).

This is the reusable engine + ontology layer the graph-agent runner reasons over,
ported from the graph-explorer sidecar (``apps/graph-explorer/src/sidecar/graph/``)
so the Python benchmark answers exactly what the TypeScript reference does. It is
fully usable and unit-testable **without** an API key — the agent loop lives in
:mod:`enterprise_sim.benchmark.runners.graph_agent`.

Two engines, two strengths, one shared
:class:`~enterprise_sim.benchmark.runners.projection.GraphModel`:

* :class:`KuzuEngine` — a typed property graph (one node table per node type, one
  rel table per edge type). Cypher does multi-hop traversal directly with
  recursive patterns (``-[:reports_to*1..]->``).
* :class:`OxigraphEngine` — the graph as RDF triples plus an **ontology**: the
  :data:`INFERENCE_RULES` (ported verbatim from ``rdf.ts``) are forward-chained to
  a fixpoint, materializing derived predicates (``der:reports_to_chain``,
  ``der:in_department``, ``der:advances_goal_effective`` …). That entailment is
  what SPARQL buys over raw traversal.

Both expose ``query`` returning rows, ``describe_schema`` for the agent's prompt,
and helpers to pull entity node ids out of results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

import kuzu
import pyoxigraph as ox

from enterprise_sim.benchmark.runners.projection import GraphModel, ModelNode

# --------------------------------------------------------------------------- #
# Namespaces — identical to apps/graph-explorer/src/sidecar/graph/rdf.ts.
# --------------------------------------------------------------------------- #

BASE = "http://enterprise-sim/"
NS = {
    "es": BASE,
    "cls": f"{BASE}class/",
    "rel": f"{BASE}rel/",
    "der": f"{BASE}derived/",
    "prop": f"{BASE}prop/",
    "ent": f"{BASE}entity/",
}

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Prefix map handed to pyoxigraph for every query/update, so neither our rules nor
# the agent's queries need to repeat PREFIX lines (a query may still declare its
# own; those take precedence).
PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    **NS,
}

# Forward-chaining inference rules, run to a fixpoint (each an idempotent SPARQL
# INSERT). Closure rules grow the store until stable. Ported verbatim from
# INFERENCE_RULES in apps/graph-explorer/src/sidecar/graph/rdf.ts; derived
# predicates live in the der: namespace so they are distinguishable from asserted
# rel: edges.
INFERENCE_RULES: list[tuple[str, str]] = [
    (
        "manages (inverse of reports_to)",
        "INSERT { ?m der:manages ?p } WHERE { ?p rel:reports_to ?m }",
    ),
    (
        "reports_to_chain (transitive base)",
        "INSERT { ?p der:reports_to_chain ?m } WHERE { ?p rel:reports_to ?m }",
    ),
    (
        "reports_to_chain (transitive step)",
        "INSERT { ?p der:reports_to_chain ?z } "
        "WHERE { ?p der:reports_to_chain ?m . ?m der:reports_to_chain ?z }",
    ),
    (
        "manages_chain (transitive closure of manages)",
        "INSERT { ?m der:manages_chain ?p } WHERE { ?p der:reports_to_chain ?m }",
    ),
    (
        "collaborates_with (symmetric)",
        "INSERT { ?b rel:collaborates_with ?a } WHERE { ?a rel:collaborates_with ?b }",
    ),
    (
        "subgoal_of_chain (transitive base)",
        "INSERT { ?a der:subgoal_of_chain ?b } WHERE { ?a rel:subgoal_of ?b }",
    ),
    (
        "subgoal_of_chain (transitive step)",
        "INSERT { ?a der:subgoal_of_chain ?c } "
        "WHERE { ?a der:subgoal_of_chain ?b . ?b der:subgoal_of_chain ?c }",
    ),
    (
        "subinitiative_of_chain (transitive base)",
        "INSERT { ?a der:subinitiative_of_chain ?b } WHERE { ?a rel:subinitiative_of ?b }",
    ),
    (
        "subinitiative_of_chain (transitive step)",
        "INSERT { ?a der:subinitiative_of_chain ?c } "
        "WHERE { ?a der:subinitiative_of_chain ?b . ?b der:subinitiative_of_chain ?c }",
    ),
    (
        "advances_goal_effective (base)",
        "INSERT { ?x der:advances_goal_effective ?g } WHERE { ?x rel:advances_goal ?g }",
    ),
    (
        "advances_goal_effective (propagate up goal tree)",
        "INSERT { ?x der:advances_goal_effective ?g2 } "
        "WHERE { ?x der:advances_goal_effective ?g . ?g der:subgoal_of_chain ?g2 }",
    ),
    (
        "in_department (via team membership)",
        "INSERT { ?p der:in_department ?dept } "
        "WHERE { ?p rel:member_of ?team . ?team rel:part_of ?dept }",
    ),
    (
        "in_department (via leadership)",
        "INSERT { ?p der:in_department ?dept } "
        "WHERE { ?p rel:leads ?dept . ?dept rdf:type cls:Department }",
    ),
]

# Derived predicates the ontology materializes — surfaced in the schema prompt.
DERIVED_PREDICATES = (
    "der:manages",
    "der:manages_chain",
    "der:reports_to_chain",
    "der:subgoal_of_chain",
    "der:subinitiative_of_chain",
    "der:advances_goal_effective",
    "der:in_department",
)


def sanitize_local(key: str) -> str:
    """Make ``key`` a safe RDF local name (sidecar ``sanitizeLocal`` parity)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def ent_iri(node_id: str) -> str:
    """The entity IRI for a node id (``ent:<urlencoded-id>``)."""
    return NS["ent"] + quote(node_id, safe="")


def _scalar_cell(value: object) -> str | None:
    """A literal string for an RDF object, or ``None`` for arrays/objects/null."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return None  # arrays/objects are kept in Cypher, skipped in RDF (sidecar parity)


# --------------------------------------------------------------------------- #
# SPARQL engine (Oxigraph) + materialized ontology.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SparqlResult:
    """A SPARQL result: SELECT rows, an ASK boolean, or CONSTRUCT triples.

    ``rows`` values and CONSTRUCT terms are *shortened* — entity IRIs back to node
    ids, schema IRIs to ``rel:``/``der:``/``cls:``/``prop:`` prefixed names — so a
    reader (or the agent) sees readable identifiers.
    """

    kind: str  # "select" | "ask" | "construct"
    columns: list[str]
    rows: list[dict[str, str]]
    boolean: bool | None = None


class OxigraphEngine:
    """Embedded SPARQL engine: the gold KG as RDF + ontology + materialized inference."""

    def __init__(self, store: ox.Store, inferred_count: int) -> None:
        self._store = store
        self.inferred_count = inferred_count

    @classmethod
    def build(cls, model: GraphModel) -> OxigraphEngine:
        """Load ``model`` into a fresh in-memory store and materialize the ontology."""
        store = ox.Store()

        def nn(value: str) -> ox.NamedNode:
            return ox.NamedNode(value)

        type_pred = nn(RDF_TYPE)
        label_pred = nn(RDFS_LABEL)
        for node in model.nodes:
            subject = nn(ent_iri(node.id))
            store.add(ox.Quad(subject, type_pred, nn(NS["cls"] + sanitize_local(node.type))))
            store.add(ox.Quad(subject, label_pred, ox.Literal(node.label)))
            for key, value in node.props.items():
                cell = _scalar_cell(value)
                if cell is None:
                    continue
                store.add(ox.Quad(subject, nn(NS["prop"] + sanitize_local(key)), ox.Literal(cell)))
        for edge in model.edges:
            store.add(
                ox.Quad(
                    nn(ent_iri(edge.src)),
                    nn(NS["rel"] + sanitize_local(edge.type)),
                    nn(ent_iri(edge.dst)),
                )
            )

        before = len(store)
        cls._materialize(store)
        return cls(store, inferred_count=len(store) - before)

    @staticmethod
    def _materialize(store: ox.Store) -> None:
        """Run :data:`INFERENCE_RULES` to a fixpoint (store size stops growing)."""
        last = -1
        guard = 0
        while len(store) != last and guard < 50:
            last = len(store)
            for _name, sparql in INFERENCE_RULES:
                store.update(sparql, prefixes=PREFIXES)
            guard += 1

    @property
    def size(self) -> int:
        """Total triples in the store (asserted + inferred)."""
        return len(self._store)

    def query(self, sparql: str) -> SparqlResult:
        """Run a SPARQL query and return shortened, readable results."""
        result = self._store.query(sparql, prefixes=PREFIXES)
        if isinstance(result, ox.QueryBoolean):
            return SparqlResult(kind="ask", columns=[], rows=[], boolean=bool(result))
        if isinstance(result, ox.QueryTriples):
            triple_rows = [
                {
                    "subject": self._short(t.subject),
                    "predicate": self._short(t.predicate),
                    "object": self._short(t.object),
                }
                for t in result
            ]
            return SparqlResult(
                kind="construct", columns=["subject", "predicate", "object"], rows=triple_rows
            )
        columns = [var.value for var in result.variables]
        rows: list[dict[str, str]] = []
        for solution in result:
            row: dict[str, str] = {}
            for col in columns:
                term = solution[col]
                row[col] = self._short(term) if term is not None else ""
            rows.append(row)
        return SparqlResult(kind="select", columns=columns, rows=rows)

    def node_ids(self, sparql: str) -> list[str]:
        """Run a SELECT and return the distinct entity node ids it binds, sorted.

        Any cell that resolves to an ``ent:`` IRI yields a node id; non-entity
        terms (literals, schema IRIs) are ignored. Used by reference queries and
        the runner to turn a query into a predicted answer set.
        """
        result = self._store.query(sparql, prefixes=PREFIXES)
        found: set[str] = set()
        if isinstance(result, ox.QueryBoolean | ox.QueryTriples):
            return []
        for solution in result:
            for var in result.variables:
                node_id = _entity_id(solution[var.value])
                if node_id is not None:
                    found.add(node_id)
        return sorted(found)

    @staticmethod
    def _short(term: object) -> str:
        """Map an RDF term back to a readable id / prefixed name for display."""
        value = getattr(term, "value", None)
        if not isinstance(value, str):
            return str(term)
        if value.startswith(NS["ent"]):
            return unquote(value[len(NS["ent"]) :])
        for key in ("rel", "der", "cls", "prop"):
            iri = NS[key]
            if value.startswith(iri):
                return f"{key}:{value[len(iri) :]}"
        return value

    def describe_schema(self, model: GraphModel) -> str:
        """Compact SPARQL schema description for the agent's system prompt."""
        classes = ", ".join(f"cls:{t}" for t in model.node_types)
        rels = ", ".join(f"rel:{t}" for t in model.edge_types)
        derived = ", ".join(DERIVED_PREDICATES)
        return "\n".join(
            [
                "Entity IRIs: ent:<urlencoded-id>  (e.g. ent:person%3Aben-cho)",
                f"Classes (rdf:type): {classes}",
                f"Asserted predicates: {rels}",
                f"Inferred predicates (materialized via the ontology): {derived}",
                "Literal props: prop:<key> (e.g. prop:name, prop:seniority, prop:kind)",
                "Labels: rdfs:label",
                f"Inferred triples added by reasoning: {self.inferred_count}",
            ]
        )


def _entity_id(term: object) -> str | None:
    """Return the node id if ``term`` is an entity IRI, else ``None``."""
    value = getattr(term, "value", None)
    if isinstance(value, str) and value.startswith(NS["ent"]):
        return unquote(value[len(NS["ent"]) :])
    return None


# --------------------------------------------------------------------------- #
# Cypher engine (Kùzu).
# --------------------------------------------------------------------------- #

# Columns we set ourselves, plus Cypher/Kùzu reserved keywords that cannot be
# bare column identifiers (sidecar parity, kuzu.ts RESERVED).
_RESERVED = frozenset(
    s.lower()
    for s in (
        "id",
        "label",
        "created_at",
        "all",
        "and",
        "asc",
        "ascending",
        "by",
        "case",
        "cast",
        "column",
        "create",
        "delete",
        "desc",
        "descending",
        "detach",
        "distinct",
        "else",
        "end",
        "ends",
        "exists",
        "false",
        "from",
        "glob",
        "group",
        "headers",
        "in",
        "install",
        "is",
        "join",
        "limit",
        "macro",
        "match",
        "merge",
        "not",
        "null",
        "on",
        "optional",
        "or",
        "order",
        "primary",
        "profile",
        "return",
        "set",
        "shortest",
        "start",
        "table",
        "then",
        "to",
        "true",
        "union",
        "unwind",
        "when",
        "where",
        "with",
        "xor",
    )
)


def sanitize_ident(key: str) -> str:
    """Make ``key`` a safe Cypher column identifier (sidecar ``sanitizeIdent`` parity)."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", key)
    if s and s[0].isdigit():
        s = "_" + s
    if s.lower() in _RESERVED:
        s = "p_" + s
    return s


def _ident_cell(value: object) -> str | None:
    """scalar → string; arrays/objects → JSON; null → None (so it is a STRING column)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    import json

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


@dataclass
class CypherResult:
    """A Cypher result: column names and rows (each a column→value dict)."""

    columns: list[str]
    rows: list[dict[str, object]]


@dataclass
class _RelTable:
    label: str
    pairs: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)


class KuzuEngine:
    """Embedded Cypher engine: the gold KG as a typed property graph.

    One node table per node type and one rel table per edge type (with every
    observed FROM/TO node-type pair); props are flattened to STRING columns so the
    agent can filter on them. Built in-memory; close to release native resources.
    """

    def __init__(self) -> None:
        self._db = kuzu.Database(":memory:")
        self._conn = kuzu.Connection(self._db)
        self.node_tables: list[tuple[str, list[str]]] = []
        self.rel_tables: list[_RelTable] = []

    @classmethod
    def build(cls, model: GraphModel) -> KuzuEngine:
        """Create the schema from ``model`` and load all nodes and edges."""
        engine = cls()
        engine._load(model)
        return engine

    def _load(self, model: GraphModel) -> None:
        nodes_by_type: dict[str, list[ModelNode]] = {}
        for node in model.nodes:
            nodes_by_type.setdefault(node.type, []).append(node)

        # ---- node tables ----
        for node_type in sorted(nodes_by_type):
            nodes = nodes_by_type[node_type]
            col_map: dict[str, str] = {}
            for node in nodes:
                for key in node.props:
                    col_map.setdefault(key, sanitize_ident(key))
            prop_cols = [f"{col} STRING" for col in col_map.values()]
            cols = ["id STRING", "label STRING", "created_at STRING", *prop_cols]
            self._conn.execute(f"CREATE NODE TABLE {node_type}({', '.join(cols)}, PRIMARY KEY(id))")
            self.node_tables.append((node_type, ["id", "label", "created_at", *col_map.values()]))
            for node in nodes:
                params: dict[str, object] = {
                    "id": node.id,
                    "label": node.label,
                    "created_at": node.created_at,
                }
                assigns = ["id: $id", "label: $label", "created_at: $created_at"]
                for key, col in col_map.items():
                    cell = _ident_cell(node.props.get(key))
                    if cell is not None:
                        params[col] = cell
                        assigns.append(f"{col}: ${col}")
                self._conn.execute(f"CREATE (n:{node_type} {{{', '.join(assigns)}}})", params)

        # ---- rel tables ----
        node_type_of = {node.id: node.type for node in model.nodes}
        edges_by_type: dict[str, list[tuple[str, str, str, str, dict[str, object]]]] = {}
        for edge in model.edges:
            edges_by_type.setdefault(edge.type, []).append(
                (edge.id, edge.src, edge.dst, edge.created_at, edge.props)
            )
        for edge_type in sorted(edges_by_type):
            edges = edges_by_type[edge_type]
            pairs: dict[tuple[str, str], None] = {}
            col_map = {}
            for _eid, src, dst, _ca, props in edges:
                s, d = node_type_of.get(src), node_type_of.get(dst)
                if s is None or d is None:
                    continue
                pairs.setdefault((s, d), None)
                for key in props:
                    col_map.setdefault(key, sanitize_ident(key))
            if not pairs:
                continue
            pair_decls = [f"FROM {s} TO {d}" for s, d in pairs]
            prop_cols = [
                "id STRING",
                "created_at STRING",
                *(f"{col} STRING" for col in col_map.values()),
            ]
            self._conn.execute(
                f"CREATE REL TABLE {edge_type}({', '.join([*pair_decls, *prop_cols])})"
            )
            self.rel_tables.append(
                _RelTable(
                    label=edge_type,
                    pairs=list(pairs),
                    columns=["id", "created_at", *col_map.values()],
                )
            )
            for eid, src, dst, created_at, props in edges:
                s, d = node_type_of.get(src), node_type_of.get(dst)
                if s is None or d is None:
                    continue
                params = {"src": src, "dst": dst, "id": eid, "created_at": created_at}
                assigns = ["id: $id", "created_at: $created_at"]
                for key, col in col_map.items():
                    cell = _ident_cell(props.get(key))
                    if cell is not None:
                        params[col] = cell
                        assigns.append(f"{col}: ${col}")
                self._conn.execute(
                    f"MATCH (a:{s} {{id: $src}}), (b:{d} {{id: $dst}}) "
                    f"CREATE (a)-[:{edge_type} {{{', '.join(assigns)}}}]->(b)",
                    params,
                )

    def query(self, cypher: str) -> CypherResult:
        """Run a Cypher query and return its columns and rows."""
        result = self._conn.execute(cypher)
        if isinstance(result, list):  # multi-statement; take the last result set
            result = result[-1]
        columns = result.get_column_names()
        rows = [dict(zip(columns, row, strict=True)) for row in result.get_all()]
        return CypherResult(columns=columns, rows=rows)

    def node_ids(self, cypher: str) -> list[str]:
        """Run a query and return the distinct string id-like cell values, sorted.

        Collects every cell from columns named ``id`` / ``*.id`` (Cypher's usual
        way to project node ids) into a sorted, de-duplicated id set.
        """
        result = self.query(cypher)
        id_cols = [c for c in result.columns if c == "id" or c.endswith(".id") or c.endswith("_id")]
        found: set[str] = set()
        for row in result.rows:
            for col in id_cols:
                value = row.get(col)
                if isinstance(value, str):
                    found.add(value)
        return sorted(found)

    def describe_schema(self) -> str:
        """Compact Cypher schema description for the agent's system prompt."""
        node_lines = "\n".join(
            f"  (:{label})  cols: {', '.join(cols)}" for label, cols in self.node_tables
        )
        rel_lines = "\n".join(
            f"  [:{t.label}]  {' | '.join(f'{s}->{d}' for s, d in t.pairs)}"
            + (f"  cols: {', '.join(t.columns)}" if len(t.columns) > 2 else "")
            for t in self.rel_tables
        )
        return f"NODE TABLES:\n{node_lines}\n\nREL TABLES:\n{rel_lines}"

    def close(self) -> None:
        """Release the in-memory database and connection."""
        self._conn.close()
        self._db.close()
