"""Actor/relationship resolver — bind a :class:`Selector` to real people.

This implements ARCHITECTURE §15.3 and decision D28: the half of Layer B's
engine that picks *who* participates. Given a declarative :class:`Selector`
(query the KG by type / attributes, exclude some ids, rank, and draw ``count``),
the resolver returns concrete people and **writes the relationship edges it
implies** back into the KG (``reviews_for``, ``collaborates_with``, …), directly
populating the relationship layer.

Collaboration realism is **affinity + capacity** (§15.3):

* **Affinity (preferential attachment).** Ranking reads a latent affinity weight
  from existing ``collaborates_with`` edges (seeded by Layer A, reinforced here),
  so go-to collaborators surface first. Picking someone *reinforces* that edge
  (its weight grows), so frequent-collaborator clusters self-organize over a run.
* **Capacity (inverse load).** A candidate's current load — how many active
  relationship edges already point at them — both **hard-caps** selection (a
  candidate at capacity is dropped) and **soft-penalises** ranking (inverse
  load), so nobody ends up on every review.
* **Expertise match.** Ranking rewards candidates whose expertise tags cover the
  required tags, so reviewer choice is realistic.

The **preferential-vs-load balance is a tunable knob** (:class:`RankWeights`).

**Determinism (D26).** Every stochastic draw — how many to pick and which —
pulls from a caller-supplied seeded :class:`random.Random` (see
:mod:`enterprise_sim.core.config.seed`); ranking ties break by node id. Same
inputs and seed therefore yield identical picks and identical written edges,
regardless of evaluation order.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from enterprise_sim.core.world import Edge, Node, World

__all__ = [
    "Filter",
    "FilterOp",
    "RankSignal",
    "RankWeights",
    "Resolution",
    "Resolver",
    "Selector",
]

FilterOp = Literal["eq", "ne", "in", "contains", "gte", "lte"]
RankSignal = Literal["affinity", "inverse_load", "expertise"]


def _node_field(node: Node, field_name: str) -> Any:
    """Read ``field_name`` from a node: ``id``/``type`` are attributes, else props."""
    if field_name == "id":
        return node.id
    if field_name == "type":
        return node.type
    return node.props.get(field_name)


@dataclass(frozen=True)
class Filter:
    """A single declarative predicate over a candidate node.

    ``field`` names a node attribute (``id``, ``type``) or a key in ``props``
    (``team``, ``seniority``, ``expertise`` …). Comparison is by ``op``:

    * ``eq`` / ``ne`` — scalar (in)equality.
    * ``gte`` / ``lte`` — ordered comparison (seniority levels, numbers).
    * ``in`` — the node's scalar value is one of ``value`` (a collection).
    * ``contains`` — the node's value is a collection that contains ``value``
      (e.g. an ``expertise`` tag list contains ``"payments"``).

    A node whose ``field`` is missing (``None``) never matches.
    """

    field: str
    op: FilterOp
    value: Any

    def matches(self, node: Node) -> bool:
        """Return ``True`` if ``node`` satisfies this predicate."""
        actual = _node_field(node, self.field)
        if actual is None:
            return False
        if self.op == "eq":
            return bool(actual == self.value)
        if self.op == "ne":
            return bool(actual != self.value)
        if self.op == "in":
            return actual in self.value
        if self.op == "contains":
            return self.value in actual
        if self.op == "gte":
            return bool(actual >= self.value)
        if self.op == "lte":
            return bool(actual <= self.value)
        raise ValueError(f"unknown filter op: {self.op!r}")


@dataclass(frozen=True)
class Selector:
    """Declarative binding spec: query the KG, exclude, rank, and draw ``count``.

    Mirrors the authoring primitive ``Selector(type, where, exclude, rank_by,
    count)`` from ARCHITECTURE §12.2; this is the engine-side representation the
    resolver evaluates (the full authoring SDK wraps it later).

    Attributes:
        type: Node type to query (e.g. ``"Person"``).
        where: Conjunction of :class:`Filter` predicates candidates must satisfy.
        exclude: Node ids to drop from the candidate set (e.g. the author).
        rank_by: Ranking signals to combine; order is informational only —
            weights live in :class:`RankWeights`. Empty means rank by all signals
            the resolver is configured for.
        expertise: Required expertise tags used by the ``expertise`` signal and
            matched against each candidate's ``expertise`` prop. Empty disables
            the expertise contribution.
        count: How many to pick — a fixed ``int`` (``2``) or a ``"lo..hi"`` range
            string (``"2..3"``) from which a seeded draw chooses ``k``.
        distinct: If ``True`` (default), never pick the same node twice in one
            resolution.
    """

    type: str
    where: tuple[Filter, ...] = ()
    exclude: tuple[str, ...] = ()
    rank_by: tuple[RankSignal, ...] = ()
    expertise: tuple[str, ...] = ()
    count: int | str = 1
    distinct: bool = True

    def count_range(self) -> tuple[int, int]:
        """Parse :attr:`count` into an inclusive ``(lo, hi)`` range.

        A bare int ``n`` yields ``(n, n)``; ``"lo..hi"`` yields ``(lo, hi)``.
        Raises :class:`ValueError` on a malformed or negative/inverted range.
        """
        if isinstance(self.count, int):
            lo = hi = self.count
        else:
            text = self.count.strip()
            if ".." in text:
                lo_s, _, hi_s = text.partition("..")
                try:
                    lo, hi = int(lo_s), int(hi_s)
                except ValueError as exc:
                    raise ValueError(f"malformed count range: {self.count!r}") from exc
            else:
                try:
                    lo = hi = int(text)
                except ValueError as exc:
                    raise ValueError(f"malformed count: {self.count!r}") from exc
        if lo < 0 or hi < lo:
            raise ValueError(f"invalid count range: {self.count!r} -> ({lo}, {hi})")
        return lo, hi


@dataclass(frozen=True)
class RankWeights:
    """Weights blending the three ranking signals (the tunable D28 knob).

    The score for a candidate is ``affinity*w_aff + inverse_load*w_load +
    expertise*w_exp`` plus a small constant floor so a zero-score candidate can
    still be drawn. Raising ``affinity`` relative to ``inverse_load`` favours
    go-to experts (preferential attachment); raising ``inverse_load`` spreads
    work (capacity). Each signal is normalised to ``[0, 1]`` before weighting so
    the weights are directly comparable.
    """

    affinity: float = 1.0
    inverse_load: float = 1.0
    expertise: float = 1.0
    floor: float = 0.05


@dataclass(frozen=True)
class Resolution:
    """The result of one :meth:`Resolver.resolve` call.

    Attributes:
        selected: The chosen nodes, in deterministic draw order (best-ranked
            first when scores differ).
        relationship_edges: Ids of the relationship edges written for this bind
            (e.g. ``reviews_for``), empty when no ``relationship`` was requested.
        affinity_edges: Ids of the ``collaborates_with`` edges created or
            reinforced for this bind.
    """

    selected: tuple[Node, ...]
    relationship_edges: tuple[str, ...] = ()
    affinity_edges: tuple[str, ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        """The selected node ids, in draw order."""
        return tuple(n.id for n in self.selected)


class Resolver:
    """Binds selectors to people and writes the relationship edges implied.

    A resolver wraps the :class:`World` it reads from and writes to, plus the
    tunable knobs (ranking weights, capacity cap, which edge types count as
    affinity and as load). One resolver is reused across many
    :meth:`resolve` calls so reinforcement accumulates in the shared graph.
    """

    def __init__(
        self,
        world: World,
        *,
        weights: RankWeights | None = None,
        capacity: int | None = None,
        affinity_edge_type: str = "collaborates_with",
        load_edge_types: Sequence[str] = ("reviews_for",),
    ) -> None:
        """Construct a resolver over ``world``.

        Args:
            world: The KG to query and to write relationship edges into.
            weights: Ranking-signal blend; defaults to equal weights.
            capacity: Hard cap on a candidate's current load — candidates at or
                above this many incident load-edges are dropped before ranking.
                ``None`` disables the cap (inverse-load still ranks softly).
            affinity_edge_type: Edge type read for affinity and reinforced on
                pick. Symmetric: stored on a canonical (sorted-pair) edge.
            load_edge_types: Edge types whose incidence counts as a candidate's
                current load for capacity and inverse-load ranking.
        """
        self._world = world
        self._weights = weights or RankWeights()
        self._capacity = capacity
        self._affinity_edge_type = affinity_edge_type
        self._load_edge_types = tuple(load_edge_types)

    # -- public API ---------------------------------------------------------

    def resolve(
        self,
        selector: Selector,
        *,
        rng: random.Random,
        at: datetime,
        anchor: str | None = None,
        relationship: str | None = None,
        reinforce: bool = True,
    ) -> Resolution:
        """Bind ``selector`` to concrete people and write the implied edges.

        Pipeline (ARCHITECTURE §15.3): candidate query → exclude (selector list,
        the ``anchor`` itself, and over-capacity) → rank (affinity + inverse load
        + expertise) → seeded draw of ``count`` (weighted, without replacement
        when ``distinct``).

        Args:
            selector: The binding spec to evaluate.
            rng: Seeded PRNG for the count draw and weighted sampling. Same seed
                and inputs → identical selection (D26).
            at: Sim-time stamped on any edges written for this bind.
            anchor: Focal node the bind is *for* (the project/author reviewers
                review, the person a collaborator collaborates with). Used for
                affinity ranking and as the endpoint of written edges. When
                ``None``, affinity is neutral and no edges are written.
            relationship: Edge type to write from each selected node to
                ``anchor`` (e.g. ``"reviews_for"``). ``None`` writes none.
            reinforce: When ``True`` and ``anchor`` is set, create/strengthen the
                symmetric affinity edge for each pick (preferential attachment).

        Returns:
            A :class:`Resolution` with the picks and any written edge ids.
        """
        candidates = self._candidates(selector, anchor)
        ranked = self._rank(candidates, selector, anchor)
        selected = self._draw(ranked, selector, rng)

        relationship_edges: list[str] = []
        affinity_edges: list[str] = []
        if anchor is not None:
            for node in selected:
                if relationship is not None:
                    relationship_edges.append(
                        self._write_relationship(relationship, node.id, anchor, at)
                    )
                if reinforce:
                    affinity_edges.append(self._reinforce(node.id, anchor, at))

        return Resolution(
            selected=tuple(selected),
            relationship_edges=tuple(relationship_edges),
            affinity_edges=tuple(affinity_edges),
        )

    # -- candidate query + exclude -----------------------------------------

    def _candidates(self, selector: Selector, anchor: str | None) -> list[Node]:
        """Query by type, apply ``where`` filters, then drop excluded/over-cap."""
        excluded = set(selector.exclude)
        if anchor is not None:
            excluded.add(anchor)
        result: list[Node] = []
        for node in self._world.nodes_by_type(selector.type):
            if node.id in excluded:
                continue
            if not all(f.matches(node) for f in selector.where):
                continue
            if self._capacity is not None and self._load(node.id) >= self._capacity:
                continue
            result.append(node)
        return result

    # -- ranking ------------------------------------------------------------

    def _rank(
        self, candidates: list[Node], selector: Selector, anchor: str | None
    ) -> list[tuple[Node, float]]:
        """Score each candidate and sort by score desc, breaking ties by id."""
        signals = self._active_signals(selector)
        affinities = {n.id: self._affinity(anchor, n.id) for n in candidates}
        max_aff = max(affinities.values(), default=0.0)

        scored: list[tuple[Node, float]] = []
        for node in candidates:
            score = self._weights.floor
            if "affinity" in signals:
                norm = affinities[node.id] / max_aff if max_aff > 0 else 0.0
                score += self._weights.affinity * norm
            if "inverse_load" in signals:
                score += self._weights.inverse_load * (1.0 / (1.0 + self._load(node.id)))
            if "expertise" in signals:
                score += self._weights.expertise * _expertise_match(selector.expertise, node)
            scored.append((node, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored

    def _active_signals(self, selector: Selector) -> frozenset[RankSignal]:
        """The signals to apply: the selector's ``rank_by`` or all three."""
        if selector.rank_by:
            return frozenset(selector.rank_by)
        return frozenset(("affinity", "inverse_load", "expertise"))

    # -- seeded draw --------------------------------------------------------

    def _draw(
        self,
        ranked: list[tuple[Node, float]],
        selector: Selector,
        rng: random.Random,
    ) -> list[Node]:
        """Draw ``count`` nodes by score-weighted sampling without replacement.

        ``k`` is a seeded draw from the selector's count range (clamped to the
        number of candidates). Sampling is weighted by score so high-affinity /
        low-load / expert candidates surface more often, yet remains seeded —
        identical ``rng`` state and inputs reproduce the same picks.
        """
        lo, hi = selector.count_range()
        available = len(ranked)
        lo = min(lo, available)
        hi = min(hi, available)
        if available == 0 or hi == 0:
            return []
        k = lo if lo == hi else rng.randint(lo, hi)

        pool = list(ranked)
        picks: list[Node] = []
        for _ in range(k):
            if not pool:
                break
            node = _weighted_pop(pool, rng)
            picks.append(node)
            if not selector.distinct:
                pool.append((node, _score_of(ranked, node.id)))
        return picks

    # -- affinity / load lookups -------------------------------------------

    def _affinity(self, anchor: str | None, candidate: str) -> float:
        """Latent affinity between ``anchor`` and ``candidate`` (0 if no anchor).

        Reads the canonical symmetric affinity edge's ``weight`` prop; absent
        edge means zero affinity.
        """
        if anchor is None:
            return 0.0
        edge = self._world.get_edge(self._affinity_edge_id(anchor, candidate))
        if edge is None:
            return 0.0
        weight = edge.props.get("weight", 1.0)
        return float(weight)

    def _load(self, node_id: str) -> int:
        """Current load: incident edges of the configured load types (both ends)."""
        total = 0
        for edge_type in self._load_edge_types:
            total += len(self._world.out_edges(node_id, edge_type))
            total += len(self._world.in_edges(node_id, edge_type))
        return total

    # -- edge writing -------------------------------------------------------

    def _write_relationship(
        self, edge_type: str, src: str, dst: str, at: datetime
    ) -> str:
        """Write a directed relationship edge ``src -> dst`` of ``edge_type``.

        The id embeds the sim-time so the same pair relating again later is a
        distinct, deterministic edge rather than a duplicate-id collision.
        """
        edge_id = f"edge:{edge_type}:{src}->{dst}@{at.isoformat()}"
        if self._world.get_edge(edge_id) is None:
            self._world.add_edge(Edge(edge_id, edge_type, src, dst, at))
        return edge_id

    def _reinforce(self, a: str, b: str, at: datetime) -> str:
        """Create or strengthen the symmetric affinity edge between ``a`` and ``b``.

        Preferential attachment: an existing edge's ``weight`` grows by one;
        otherwise a fresh edge is created with ``weight = 1`` stamped at ``at``.
        """
        edge_id = self._affinity_edge_id(a, b)
        edge = self._world.get_edge(edge_id)
        if edge is None:
            src, dst = sorted((a, b))
            self._world.add_edge(
                Edge(edge_id, self._affinity_edge_type, src, dst, at, props={"weight": 1.0})
            )
        else:
            edge.props["weight"] = float(edge.props.get("weight", 1.0)) + 1.0
        return edge_id

    def _affinity_edge_id(self, a: str, b: str) -> str:
        """Canonical (order-independent) id for the affinity edge between a/b."""
        src, dst = sorted((a, b))
        return f"edge:{self._affinity_edge_type}:{src}<->{dst}"


def _expertise_match(required: Iterable[str], node: Node) -> float:
    """Fraction of ``required`` tags present in the node's ``expertise`` prop.

    Returns ``1.0`` when nothing is required (the signal is inert) and ``0.0``
    when the node has no expertise tags.
    """
    required_set = set(required)
    if not required_set:
        return 1.0
    have = node.props.get("expertise", [])
    if not isinstance(have, (list, tuple, set)):
        have = [have]
    matched = required_set.intersection(have)
    return len(matched) / len(required_set)


def _score_of(ranked: Sequence[tuple[Node, float]], node_id: str) -> float:
    """Look up the precomputed score for ``node_id`` in a ranked list."""
    for node, score in ranked:
        if node.id == node_id:
            return score
    return 0.0


def _weighted_pop(pool: list[tuple[Node, float]], rng: random.Random) -> Node:
    """Remove and return one node from ``pool``, sampled proportional to score.

    Scores are non-negative (a positive floor guarantees it); when every weight
    is zero the choice is uniform. The pool is kept in deterministic (score
    desc, id) order by the caller, so a given ``rng`` state always lands on the
    same element.
    """
    total = sum(max(score, 0.0) for _, score in pool)
    if total <= 0.0:
        index = rng.randrange(len(pool))
        return pool.pop(index)[0]
    target = rng.random() * total
    cumulative = 0.0
    for index, (_node, score) in enumerate(pool):
        cumulative += max(score, 0.0)
        if target < cumulative:
            return pool.pop(index)[0]
    # Floating-point guard: return the last element if rounding overshoots.
    return pool.pop()[0]
