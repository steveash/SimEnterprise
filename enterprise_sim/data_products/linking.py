"""Bind data-scenario dimensions to the simulated enterprise's knowledge graph.

The user's enterprise already exists — Layer A built it and the run exported it
as ``kg/nodes.jsonl`` / ``kg/edges.jsonl``. This module makes the structured
data *about that same company*: populations can be bound 1:1 to KG nodes
(:class:`~enterprise_sim.data_products.spec.KgIdentity` — e.g. one row per
sales rep Person) and categorical dimensions can draw their levels from KG
nodes (:class:`~enterprise_sim.data_products.spec.KgDimension` — e.g. each
user's ``team`` is a real Team node). Every binding is recorded as lineage so
the parquet values are traceable back to gold KG entity ids.

Deterministic: assignment draws come from seeded ``random.Random`` sub-streams
(``SeedContext``), so the same world + seed reproduces the same bindings.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.data_products.spec import KgDimension, KgIdentity

__all__ = [
    "DimensionBinding",
    "IdentityBinding",
    "LinkageError",
    "assign_dimension",
    "bind_identity",
    "load_world_from_run",
    "match_nodes",
]


class LinkageError(Exception):
    """Raised when a KG binding cannot be satisfied by the supplied world."""


def load_world_from_run(run_dir: str | Path) -> World:
    """Load the exported gold KG of a completed enterprise-sim run.

    Reads ``<run_dir>/kg/nodes.jsonl`` (required) and ``kg/edges.jsonl``
    (optional — dimensions only need nodes, but edges are loaded when present
    so future bindings can traverse relationships).
    """
    run_dir = Path(run_dir)
    nodes_path = run_dir / "kg" / "nodes.jsonl"
    if not nodes_path.is_file():
        raise LinkageError(f"no exported KG at {nodes_path} (is this a completed run directory?)")
    world = World()
    with nodes_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                world.add_node(Node.from_dict(json.loads(line)))
    edges_path = run_dir / "kg" / "edges.jsonl"
    if edges_path.is_file():
        with edges_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    world.add_edge(Edge.from_dict(json.loads(line)))
    return world


def match_nodes(world: World, node_type: str, where: dict[str, str]) -> list[Node]:
    """All nodes of ``node_type`` whose props exact-match every ``where`` entry.

    Ordered by id, so downstream seeded draws are deterministic regardless of
    insertion order.
    """
    matched = [
        node
        for node in world.nodes_by_type(node_type)
        if all(str(node.props.get(key)) == value for key, value in where.items())
    ]
    return sorted(matched, key=lambda node: node.id)


def _display_name(node: Node) -> str:
    name = node.props.get("name")
    return str(name) if name else node.id


@dataclass(frozen=True, slots=True)
class IdentityBinding:
    """A population bound 1:1 to KG nodes: parallel ids and display names."""

    kg_ids: tuple[str, ...]
    kg_names: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.kg_ids)


def bind_identity(identity: KgIdentity, world: World) -> IdentityBinding:
    """Resolve a :class:`KgIdentity` to the concrete node rows it maps onto."""
    nodes = match_nodes(world, identity.node_type, identity.where)
    if not nodes:
        raise LinkageError(
            f"kg_identity matched no {identity.node_type!r} nodes (where={identity.where})"
        )
    return IdentityBinding(
        kg_ids=tuple(node.id for node in nodes),
        kg_names=tuple(_display_name(node) for node in nodes),
    )


@dataclass(frozen=True, slots=True)
class DimensionBinding:
    """A KG-backed categorical dimension: per-entity values plus lineage.

    ``values`` holds each entity's assigned level (a node display name);
    ``level_to_id`` maps every level back to its KG node id.
    """

    attribute: str
    values: tuple[str, ...]
    level_to_id: dict[str, str]

    @property
    def levels(self) -> tuple[str, ...]:
        return tuple(sorted(self.level_to_id))


def assign_dimension(
    dim: KgDimension, world: World, *, size: int, rng: random.Random
) -> DimensionBinding:
    """Assign each of ``size`` entities a KG node level for dimension ``dim``.

    ``zipf`` weighting ranks the matched nodes in a seeded shuffle and weights
    level popularity by ``1/rank`` — big teams/products dominate, like real
    dimensions do; ``uniform`` spreads entities evenly.
    """
    nodes = match_nodes(world, dim.node_type, dim.where)
    if not nodes:
        raise LinkageError(
            f"kg_dimension {dim.attribute!r} matched no {dim.node_type!r} nodes (where={dim.where})"
        )
    ranked = list(nodes)
    rng.shuffle(ranked)
    if dim.weighting == "zipf":
        weights = [1.0 / (rank + 1) for rank in range(len(ranked))]
    else:
        weights = [1.0] * len(ranked)
    names = [_display_name(node) for node in ranked]
    values = rng.choices(names, weights=weights, k=size)
    return DimensionBinding(
        attribute=dim.attribute,
        values=tuple(values),
        level_to_id={_display_name(node): node.id for node in ranked},
    )
