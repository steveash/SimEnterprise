"""The extraction target ontology: the gold KG's node & relation vocabulary.

Schema-guided extraction (esim-nc6.3) only works because the target schema is
*known* — we are reconstructing a graph whose type system we already own, so the
extractor never does open-domain IE. This module is that type system, pinned as a
single source of truth for the reconstruct side: the entity types and relation
labels the extractor is allowed to emit, mirroring the gold KG's vocabulary
(ARCHITECTURE.md §3/§11, the same ``node.type`` / ``edge.type`` strings the
benchmark's :class:`~enterprise_sim.benchmark.runners.projection.GraphModel`
derives from the gold :class:`~enterprise_sim.core.world.World`).

Two frozen sets bound the extractor:

* :data:`NODE_TYPES` — the entity labels (``Person``, ``Team``, … ``CalendarEvent``),
  matching the gold builders' node-type constants
  (:mod:`enterprise_sim.world_builders.builder`, the ``Artifact`` producers, the
  scheduler's derived ``CalendarEvent`` nodes).
* :data:`RELATION_TYPES` — the **text-assertable** gold relations: the org / goal /
  authorship edges a reader could state or strongly infer from prose. The gold
  graph also carries *mechanical* edges that are not extracted from text — ``mentions``
  (surface-form tagging), ``expresses`` (artifact→goal projection), and
  ``has_calendar_event`` (derived from the busy map). Those are reconstructed by
  other stages, so they are deliberately excluded here to keep the extractor's
  vocabulary the set of relations a language model can actually read off a chunk.

Keeping this list in lockstep with the gold vocabulary matters: a reconstructed
edge type outside the gold vocabulary can never match a gold edge, so the fidelity
scorer (esim-nc6.6) would score it as noise. The scaffold's tests assert this set
is a subset of the gold constants, so a rename on the gold side fails loudly here.
"""

from __future__ import annotations

__all__ = [
    "NODE_TYPES",
    "RELATION_GLOSSES",
    "RELATION_TYPES",
    "describe_ontology",
    "is_known_node_type",
    "is_known_relation",
]


#: Entity labels the extractor may assign to a mention (gold ``node.type`` values).
NODE_TYPES: frozenset[str] = frozenset(
    {
        "Company",
        "Department",
        "Team",
        "Person",
        "Goal",
        "Initiative",
        "Project",
        "Artifact",
        "CalendarEvent",
    }
)

#: The text-assertable gold relations, each with a one-line gloss of its shape.
#: Ordered domain→range hints mirror the gold builders' edge comments so the
#: extractor's system prompt can teach the model each relation's direction.
RELATION_GLOSSES: dict[str, str] = {
    "reports_to": "Person → their manager (Person).",
    "member_of": "Person → a Team or Project they belong to.",
    "leads": "Person → a Team or Department they lead.",
    "owns": "Company → a Goal it owns.",
    "owns_initiative": "Person → an Initiative they own.",
    "advances_goal": "Department or Initiative → a Goal it advances.",
    "part_of": "Team → the Department it is part of.",
    "has_department": "Company → one of its Departments.",
    "subgoal_of": "Goal → its parent Goal.",
    "subinitiative_of": "Initiative → its parent Initiative.",
    "under": "Project → the Initiative it sits under.",
    "collaborates_with": "Person ↔ Person who work closely together.",
    "authored": "Person → an Artifact they authored.",
    "reviewed": "Person → an Artifact they reviewed.",
    "references": "Artifact → another Artifact it references.",
}

#: Relation labels the extractor may assign to a candidate triple (gold
#: ``edge.type`` values, restricted to those assertable from text).
RELATION_TYPES: frozenset[str] = frozenset(RELATION_GLOSSES)


def is_known_node_type(type_: str) -> bool:
    """Return whether ``type_`` is a valid ontology entity label."""
    return type_ in NODE_TYPES


def is_known_relation(rel: str) -> bool:
    """Return whether ``rel`` is a valid ontology relation label."""
    return rel in RELATION_TYPES


def describe_ontology() -> str:
    """Return a compact, stable prose description of the ontology for a prompt.

    Lists the entity types then the relations (each with its gloss), in a fixed
    sorted order so the extractor's system prompt — and therefore its prompt-cache
    key — is deterministic across runs.
    """
    entities = ", ".join(sorted(NODE_TYPES))
    relation_lines = "\n".join(
        f"- {rel}: {RELATION_GLOSSES[rel]}" for rel in sorted(RELATION_TYPES)
    )
    return f"Entity types: {entities}\n\nRelation types:\n{relation_lines}"
