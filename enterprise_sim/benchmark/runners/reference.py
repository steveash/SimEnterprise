"""Hand-written reference queries: the gold path for each reasoning type (esim-uzc.4).

Every benchmark reasoning family has a *canonical* way to answer it from the graph
— a single Cypher and a single SPARQL query that, given the question's subject node
id, return exactly the expected node ids. These reference queries are the keyless
proof that the engines + ontology answer correctly (no agent, no API key): a test
runs them through :class:`~enterprise_sim.benchmark.runners.engines.KuzuEngine` /
:class:`~enterprise_sim.benchmark.runners.engines.OxigraphEngine` and checks the
result against the gold answer computed straight from the
:class:`~enterprise_sim.core.world.World`.

They double as worked examples in the agent's system prompt: this is how the
materialized ontology (``der:reports_to_chain``, ``der:in_department``,
``der:advances_goal_effective``) turns a multi-hop question into one query.

Each :class:`Reference` builds its two queries from a subject id; the SPARQL form
uses :func:`~enterprise_sim.benchmark.runners.engines.ent_iri`-style prefixed names
so it round-trips through the same namespaces the engine loads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote


def _ent(node_id: str) -> str:
    """A SPARQL prefixed-name term for a node id (``ent:<urlencoded>``)."""
    return "ent:" + quote(node_id, safe="")


@dataclass(frozen=True)
class Reference:
    """The canonical Cypher + SPARQL answer for one reasoning pattern.

    Attributes:
        key: Short identifier (e.g. ``reports_to``, ``management_chain``).
        reasoning_type: The :data:`~enterprise_sim.benchmark.schema.REASONING_TYPES`
            family this pattern answers.
        description: One-line human description of the question it answers.
        cypher: ``subject id -> Cypher`` returning an ``id`` column of the answer.
        sparql: ``subject id -> SPARQL`` SELECT binding the answer to entity IRIs.
    """

    key: str
    reasoning_type: str
    description: str
    cypher: Callable[[str], str]
    sparql: Callable[[str], str]


REFERENCES: tuple[Reference, ...] = (
    Reference(
        key="reports_to",
        reasoning_type="direct_relation",
        description="who a person directly reports to",
        cypher=lambda s: f'MATCH (p:Person {{id: "{s}"}})-[:reports_to]->(m) RETURN m.id AS id',
        sparql=lambda s: f"SELECT ?m WHERE {{ {_ent(s)} rel:reports_to ?m }}",
    ),
    Reference(
        key="management_chain",
        reasoning_type="transitive",
        description="a person's full management chain (skip-levels included)",
        cypher=lambda s: (
            f'MATCH (p:Person {{id: "{s}"}})-[:reports_to*1..]->(m) RETURN DISTINCT m.id AS id'
        ),
        sparql=lambda s: f"SELECT ?m WHERE {{ {_ent(s)} der:reports_to_chain ?m }}",
    ),
    Reference(
        key="in_department",
        reasoning_type="transitive",
        description="which department a person sits in (via team membership/leadership)",
        cypher=lambda s: (
            f'MATCH (p:Person {{id: "{s}"}})-[:member_of]->(:Team)-[:part_of]->(d:Department) '
            f"RETURN DISTINCT d.id AS id"
        ),
        sparql=lambda s: f"SELECT ?d WHERE {{ {_ent(s)} der:in_department ?d }}",
    ),
    Reference(
        key="team_headcount",
        reasoning_type="aggregation",
        description="the people who are members of a team (count = size of the set)",
        cypher=lambda s: f'MATCH (p:Person)-[:member_of]->(t {{id: "{s}"}}) RETURN p.id AS id',
        sparql=lambda s: f"SELECT ?p WHERE {{ ?p rel:member_of {_ent(s)} }}",
    ),
    Reference(
        key="goal_advancers",
        reasoning_type="goal_tree",
        description="everything advancing a goal, directly or via its subgoals",
        cypher=lambda s: (
            f'MATCH (target)-[:subgoal_of*0..]->(:Goal {{id: "{s}"}}) '
            f"MATCH (x)-[:advances_goal]->(target) RETURN DISTINCT x.id AS id"
        ),
        sparql=lambda s: f"SELECT ?x WHERE {{ ?x der:advances_goal_effective {_ent(s)} }}",
    ),
    Reference(
        key="provenance",
        reasoning_type="provenance",
        description="which artifacts mention/ground an entity",
        cypher=lambda s: f'MATCH (a)-[:mentions]->(e {{id: "{s}"}}) RETURN a.id AS id',
        sparql=lambda s: f"SELECT ?a WHERE {{ ?a rel:mentions {_ent(s)} }}",
    ),
)

REFERENCES_BY_KEY: dict[str, Reference] = {ref.key: ref for ref in REFERENCES}
