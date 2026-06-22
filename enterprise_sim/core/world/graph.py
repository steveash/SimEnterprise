"""In-memory labeled property graph store (the KG spine).

This implements the in-memory representation from ARCHITECTURE §11.1 and decision
D18: a hand-rolled, typed labeled property graph (LPG) with **reified edges**
(first-class, id-bearing), per-element ``created_at`` sim-time, traversal indexes,
and timestamped projections. The store is the source of truth used identically in
memory and on disk; on-disk JSONL/Neo4j export lives in a separate layer.

Three design decisions encoded here (ARCHITECTURE §11.1):

1. **Edges are reified.** Every :class:`Edge` carries its own ``id`` and lives in
   its own table with adjacency indexes (``by_src`` / ``by_dst``), so provenance
   and KG-eval can target a *relationship*, not just a node.
2. **Everything carries ``created_at`` (sim-time).** A :meth:`World.projection` at
   time ``T`` is the subgraph of nodes/edges with ``created_at <= T`` (optionally
   restricted to a focus subgraph and a time window) — a producer cannot reference
   the future or an out-of-scope entity.
3. **Deterministic ids and ordering.** Storage is insertion-ordered; queries and
   serialization sort by ``id``. Same inputs → identical graph, ids, and order.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

__all__ = ["Edge", "Event", "Node", "World", "WorldView", "Direction"]

Direction = Literal["out", "in", "both"]


def _iso(value: datetime) -> str:
    """Serialize a datetime to a deterministic ISO-8601 string."""
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 string back into a datetime."""
    return datetime.fromisoformat(value)


@dataclass
class Node:
    """A typed graph node with arbitrary properties and surface-form aliases.

    Attributes:
        id: Stable, human-readable, content-derived identifier (e.g.
            ``person:ada-lovelace``). Everything else references this id.
        type: The node label (``Company``, ``Person``, ``Artifact`` …).
        created_at: Sim-time at which the node came into existence.
        props: Free-form typed properties for this node.
        aliases: Known surface forms for this entity (canonical name + aliases).
    """

    id: str
    type: str
    created_at: datetime
    props: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (matches ``kg/nodes.jsonl`` shape)."""
        return {
            "id": self.id,
            "type": self.type,
            "created_at": _iso(self.created_at),
            "props": copy.deepcopy(self.props),
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Node:
        """Reconstruct a :class:`Node` from :meth:`to_dict` output."""
        return cls(
            id=data["id"],
            type=data["type"],
            created_at=_parse_dt(data["created_at"]),
            props=copy.deepcopy(dict(data.get("props", {}))),
            aliases=list(data.get("aliases", [])),
        )


@dataclass
class Edge:
    """A reified (first-class, id-bearing) typed relationship between two nodes.

    Attributes:
        id: Stable, content-derived identifier (e.g. ``edge:reviewed:ada:dd-7``).
        type: The relationship label (``reports_to``, ``authored``, ``reviewed`` …).
        src: Source node id.
        dst: Destination node id.
        created_at: Sim-time at which the relationship came into existence.
        props: Free-form typed properties for this edge.
    """

    id: str
    type: str
    src: str
    dst: str
    created_at: datetime
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (matches ``kg/edges.jsonl`` shape)."""
        return {
            "id": self.id,
            "type": self.type,
            "src": self.src,
            "dst": self.dst,
            "created_at": _iso(self.created_at),
            "props": copy.deepcopy(self.props),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Edge:
        """Reconstruct an :class:`Edge` from :meth:`to_dict` output."""
        return cls(
            id=data["id"],
            type=data["type"],
            src=data["src"],
            dst=data["dst"],
            created_at=_parse_dt(data["created_at"]),
            props=copy.deepcopy(dict(data.get("props", {}))),
        )


@dataclass
class Event:
    """An entry in the append-only temporal journal (ARCHITECTURE §11.2).

    Events *apply* to the graph (Layer B adds nodes/edges), while the journal
    itself is retained for replay and temporal ground truth.

    Attributes:
        id: Stable, content-derived identifier.
        type: Event type (``DeliverableDrafted``, ``CommentPosted`` …).
        timestamp: Sim-time at which the event occurred.
        actors: Role → list of person ids (e.g. ``{"author": ["person:ada"]}``).
        subjects: KG node ids this event is "about".
        parent_event: Optional parent event id for threading / causal chains.
        payload: Semantic brief for the producer (topic, intent, tone …).
    """

    id: str
    type: str
    timestamp: datetime
    actors: dict[str, list[str]] = field(default_factory=dict)
    subjects: list[str] = field(default_factory=list)
    parent_event: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (matches ``kg/events.jsonl`` shape)."""
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": _iso(self.timestamp),
            "actors": {role: list(ids) for role, ids in self.actors.items()},
            "subjects": list(self.subjects),
            "parent_event": self.parent_event,
            "payload": copy.deepcopy(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        """Reconstruct an :class:`Event` from :meth:`to_dict` output."""
        return cls(
            id=data["id"],
            type=data["type"],
            timestamp=_parse_dt(data["timestamp"]),
            actors={role: list(ids) for role, ids in dict(data.get("actors", {})).items()},
            subjects=list(data.get("subjects", [])),
            parent_event=data.get("parent_event"),
            payload=copy.deepcopy(dict(data.get("payload", {}))),
        )


def _copy_node(node: Node) -> Node:
    return replace(node, props=copy.deepcopy(node.props), aliases=list(node.aliases))


def _copy_edge(edge: Edge) -> Edge:
    return replace(edge, props=copy.deepcopy(edge.props))


def _copy_event(event: Event) -> Event:
    return replace(
        event,
        actors={role: list(ids) for role, ids in event.actors.items()},
        subjects=list(event.subjects),
        payload=copy.deepcopy(event.payload),
    )


class World:
    """A typed, in-memory labeled property graph plus its temporal journal.

    Holds three keyed, insertion-ordered collections (nodes, edges, events) and
    traversal indexes: ``nodes_by_type`` / ``edges_by_type`` and the reified-edge
    adjacency indexes ``by_src`` / ``by_dst`` (node id → edge type → edge ids).

    Storage preserves insertion order for determinism; all query helpers and
    serialization sort by id so the same inputs always yield identical output.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._events: dict[str, Event] = {}
        # Type indexes: type -> [ids] (insertion order).
        self._nodes_by_type: dict[str, list[str]] = {}
        self._edges_by_type: dict[str, list[str]] = {}
        # Reified-edge adjacency: node id -> edge type -> [edge ids].
        self._by_src: dict[str, dict[str, list[str]]] = {}
        self._by_dst: dict[str, dict[str, list[str]]] = {}

    # -- mutation -----------------------------------------------------------

    def add_node(self, node: Node) -> Node:
        """Insert a node. Raises :class:`ValueError` on duplicate id."""
        if node.id in self._nodes:
            raise ValueError(f"duplicate node id: {node.id!r}")
        self._nodes[node.id] = node
        self._nodes_by_type.setdefault(node.type, []).append(node.id)
        return node

    def add_edge(self, edge: Edge) -> Edge:
        """Insert a reified edge and update the adjacency indexes.

        Endpoint existence is intentionally *not* enforced here: dangling-ref
        detection is the consistency validator's job (ARCHITECTURE §11.4 / D17),
        keeping this store free of validation policy.

        Raises :class:`ValueError` on duplicate id.
        """
        if edge.id in self._edges:
            raise ValueError(f"duplicate edge id: {edge.id!r}")
        self._edges[edge.id] = edge
        self._edges_by_type.setdefault(edge.type, []).append(edge.id)
        self._by_src.setdefault(edge.src, {}).setdefault(edge.type, []).append(edge.id)
        self._by_dst.setdefault(edge.dst, {}).setdefault(edge.type, []).append(edge.id)
        return edge

    def add_event(self, event: Event) -> Event:
        """Append an event to the journal. Raises on duplicate id."""
        if event.id in self._events:
            raise ValueError(f"duplicate event id: {event.id!r}")
        self._events[event.id] = event
        return event

    # -- single-item access -------------------------------------------------

    def get_node(self, node_id: str) -> Node | None:
        """Return the node with ``node_id`` or ``None``."""
        return self._nodes.get(node_id)

    def get_edge(self, edge_id: str) -> Edge | None:
        """Return the edge with ``edge_id`` or ``None``."""
        return self._edges.get(edge_id)

    def get_event(self, event_id: str) -> Event | None:
        """Return the event with ``event_id`` or ``None``."""
        return self._events.get(event_id)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    # -- bulk access (insertion order) -------------------------------------

    def nodes(self) -> list[Node]:
        """All nodes in insertion order."""
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        """All edges in insertion order."""
        return list(self._edges.values())

    def events(self) -> list[Event]:
        """All events in insertion order."""
        return list(self._events.values())

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    @property
    def event_count(self) -> int:
        return len(self._events)

    # -- typed queries (sorted by id) --------------------------------------

    def nodes_by_type(self, node_type: str) -> list[Node]:
        """All nodes of ``node_type``, sorted by id."""
        ids = self._nodes_by_type.get(node_type, [])
        return [self._nodes[i] for i in sorted(ids)]

    def edges_by_type(self, edge_type: str) -> list[Edge]:
        """All edges of ``edge_type``, sorted by id."""
        ids = self._edges_by_type.get(edge_type, [])
        return [self._edges[i] for i in sorted(ids)]

    def node_types(self) -> list[str]:
        """All distinct node types present, sorted."""
        return sorted(self._nodes_by_type)

    def edge_types(self) -> list[str]:
        """All distinct edge types present, sorted."""
        return sorted(self._edges_by_type)

    # -- adjacency / traversal (sorted by id) ------------------------------

    def out_edges(self, node_id: str, edge_type: str | None = None) -> list[Edge]:
        """Outgoing edges from ``node_id`` (optionally of one type), sorted by id."""
        return self._adjacent_edges(self._by_src, node_id, edge_type)

    def in_edges(self, node_id: str, edge_type: str | None = None) -> list[Edge]:
        """Incoming edges to ``node_id`` (optionally of one type), sorted by id."""
        return self._adjacent_edges(self._by_dst, node_id, edge_type)

    def _adjacent_edges(
        self,
        index: dict[str, dict[str, list[str]]],
        node_id: str,
        edge_type: str | None,
    ) -> list[Edge]:
        type_map = index.get(node_id, {})
        types = [edge_type] if edge_type is not None else list(type_map)
        edges = [self._edges[eid] for t in types for eid in type_map.get(t, [])]
        return sorted(edges, key=lambda e: e.id)

    def neighbors(
        self,
        node_id: str,
        edge_type: str | None = None,
        direction: Direction = "out",
    ) -> list[Node]:
        """Return adjacent nodes reachable from ``node_id``, sorted by id.

        Args:
            node_id: The node whose neighbors to return.
            edge_type: Restrict to one edge type, or ``None`` for all types.
            direction: ``"out"`` (follow src→dst), ``"in"`` (dst→src), or
                ``"both"``.

        Only neighbors that exist as nodes in this store are returned; dangling
        edge endpoints are skipped.
        """
        if direction not in ("out", "in", "both"):
            raise ValueError(f"invalid direction: {direction!r}")
        ids: set[str] = set()
        if direction in ("out", "both"):
            for edge in self.out_edges(node_id, edge_type):
                if edge.dst in self._nodes:
                    ids.add(edge.dst)
        if direction in ("in", "both"):
            for edge in self.in_edges(node_id, edge_type):
                if edge.src in self._nodes:
                    ids.add(edge.src)
        return [self._nodes[i] for i in sorted(ids)]

    # -- timestamped projection (ARCHITECTURE §11.1) -----------------------

    def projection(
        self,
        at: datetime,
        focus: Iterable[str] | None = None,
        window: timedelta | None = None,
    ) -> WorldView:
        """Return a timestamped projection (``WorldView``) of this graph.

        The projection is the subgraph of elements with ``created_at <= at``
        (events use ``timestamp``), optionally narrowed:

        * ``focus`` — a set of node ids to keep. Edges are kept only when **both**
          endpoints are in the focus set; events are kept only when they reference
          (via ``subjects`` or ``actors``) at least one focus node.
        * ``window`` — a lookback ``timedelta``; only elements stamped within
          ``[at - window, at]`` are kept.

        The returned view is an independent copy (mutating it does not affect this
        store), so a producer cannot reference the future or an out-of-scope
        entity.
        """
        lower = at - window if window is not None else None
        focus_set = set(focus) if focus is not None else None

        def in_time(stamp: datetime) -> bool:
            if stamp > at:
                return False
            return not (lower is not None and stamp < lower)

        view = World()
        for node in self._nodes.values():
            if not in_time(node.created_at):
                continue
            if focus_set is not None and node.id not in focus_set:
                continue
            view.add_node(_copy_node(node))

        for edge in self._edges.values():
            if not in_time(edge.created_at):
                continue
            if focus_set is not None and (edge.src not in focus_set or edge.dst not in focus_set):
                continue
            view.add_edge(_copy_edge(edge))

        for event in self._events.values():
            if not in_time(event.timestamp):
                continue
            if focus_set is not None and not _event_touches(event, focus_set):
                continue
            view.add_event(_copy_event(event))

        return view

    # -- JSON serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict with nodes/edges/events lists, each sorted by id."""
        return {
            "nodes": [n.to_dict() for n in sorted(self._nodes.values(), key=lambda n: n.id)],
            "edges": [e.to_dict() for e in sorted(self._edges.values(), key=lambda e: e.id)],
            "events": [e.to_dict() for e in sorted(self._events.values(), key=lambda e: e.id)],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialize to a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> World:
        """Reconstruct a :class:`World` from :meth:`to_dict` output."""
        world = cls()
        for node in data.get("nodes", []):
            world.add_node(Node.from_dict(node))
        for edge in data.get("edges", []):
            world.add_edge(Edge.from_dict(edge))
        for event in data.get("events", []):
            world.add_event(Event.from_dict(event))
        return world

    @classmethod
    def from_json(cls, text: str) -> World:
        """Reconstruct a :class:`World` from a JSON string."""
        return cls.from_dict(json.loads(text))


def _event_touches(event: Event, focus_set: set[str]) -> bool:
    """Return True if the event references any node id in ``focus_set``."""
    if focus_set.intersection(event.subjects):
        return True
    return any(actor in focus_set for ids in event.actors.values() for actor in ids)


# A projection is structurally a read-only snapshot of a World. We reuse the
# World class for it (same query + serialization surface) and expose this alias
# so downstream code can name the contract a producer receives (ARCHITECTURE §6).
WorldView = World
