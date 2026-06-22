"""Engine-side declarative spec the scheduler consumes (ARCHITECTURE §12/§15).

The authoring SDK (declarative Python — a later milestone, esim-3c9cfc55) is what
authors *write*; this module is what the engine *runs*. It is the minimal,
typed representation of the same primitives — the six **triggers**, a
:class:`Process` of timed :class:`Step` s, and an :class:`Activation` wiring a
process to a trigger and a role binding — exactly mirroring the resolver's
engine-side :class:`~enterprise_sim.core.sim.resolver.Selector`. Keeping the
engine's contract here (rather than in the authoring layer) lets the scheduler be
built and tested before the SDK exists, and lets the SDK later *produce* these
objects without the engine changing.

Nothing here executes; these are frozen, hashable description objects. Timing
strings (``at="day 2"``, ``duration="3d"``, ``offset="1d"``) are parsed against a
:class:`~enterprise_sim.core.sim.calendar.WorkingCalendar` at schedule time, so
*working-time* arithmetic (D27) stays the calendar's single responsibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from enterprise_sim.core.events import Deliverable
from enterprise_sim.core.sim.resolver import FilterOp, Selector
from enterprise_sim.core.world import World

__all__ = [
    "Activation",
    "Condition",
    "Effect",
    "EffectKind",
    "EventPredicate",
    "OnCadence",
    "OnCondition",
    "OnEvent",
    "OnMilestone",
    "OnStart",
    "Probabilistic",
    "Process",
    "RoleSpec",
    "Scenario",
    "Spread",
    "Step",
    "Trigger",
    "parse_business_days",
    "parse_duration_hours",
    "parse_int_range",
]


# --------------------------------------------------------------------------- #
# Timing string parsers (resolved against the calendar's hours_per_day).
# --------------------------------------------------------------------------- #


def parse_business_days(text: str) -> int:
    """Parse an ``at`` offset like ``"day 2"`` (or a bare ``"2"``) to business days.

    The offset is measured in **working days** from the instance start; ``"day 0"``
    (the default) means "at the moment the process instance fires".
    """
    cleaned = text.strip().lower()
    if cleaned.startswith("day"):
        cleaned = cleaned[len("day") :].strip()
    try:
        days = int(cleaned)
    except ValueError as exc:
        raise ValueError(f"malformed 'at' offset: {text!r}") from exc
    if days < 0:
        raise ValueError(f"'at' offset must be non-negative: {text!r}")
    return days


def parse_duration_hours(text: str, hours_per_day: float) -> float:
    """Parse a duration like ``"2h"`` / ``"1d"`` / ``"1.5d"`` to working-hours.

    ``Nd`` is ``N`` working days (``N * hours_per_day``); ``Nh`` (or a bare number)
    is ``N`` working hours. ``hours_per_day`` comes from the calendar so a "day"
    always means one working day, never 24h.
    """
    cleaned = text.strip().lower()
    if not cleaned:
        raise ValueError("empty duration")
    unit = cleaned[-1]
    if unit == "d":
        value = float(cleaned[:-1])
        hours = value * hours_per_day
    elif unit == "h":
        hours = float(cleaned[:-1])
    else:
        hours = float(cleaned)
    if hours < 0:
        raise ValueError(f"duration must be non-negative: {text!r}")
    return hours


def parse_int_range(spec: int | str) -> tuple[int, int]:
    """Parse a count spec — ``3`` or ``"1..3"`` — into an inclusive ``(lo, hi)``.

    Mirrors :meth:`Selector.count_range` for the per-actor comment counts a
    :class:`Spread` distributes; kept standalone so the spec layer has no
    dependency on the resolver's selector internals.
    """
    if isinstance(spec, int):
        lo = hi = spec
    else:
        text = spec.strip()
        if ".." in text:
            lo_s, _, hi_s = text.partition("..")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ValueError(f"malformed count range: {spec!r}") from exc
        else:
            try:
                lo = hi = int(text)
            except ValueError as exc:
                raise ValueError(f"malformed count: {spec!r}") from exc
    if lo < 0 or hi < lo:
        raise ValueError(f"invalid count range: {spec!r} -> ({lo}, {hi})")
    return lo, hi


# --------------------------------------------------------------------------- #
# Effects (KG mutations a step applies — ARCHITECTURE §12.2 effects=[...]).
# --------------------------------------------------------------------------- #


class EffectKind(StrEnum):
    """The kinds of KG mutation a step can declare."""

    CREATE_NODE = "create_node"
    MUTATE = "mutate"
    ADD_EDGE = "add_edge"
    MILESTONE = "milestone"


@dataclass(frozen=True, slots=True)
class Effect:
    """A declarative KG mutation applied when its step's event is emitted.

    Construct via the classmethods rather than the raw fields. The relevant
    fields depend on :attr:`kind`:

    * ``CREATE_NODE`` — ``target`` (new node id), ``node_type``, ``props``.
    * ``MUTATE`` — ``target`` (existing node id), ``attr``, ``value``. An
      ``OnCondition`` watching ``attr`` re-evaluates the moment this lands
      (the effect-driven path of §15.1).
    * ``ADD_EDGE`` — ``target`` (edge id), ``edge_type``, ``src``, ``dst``.
    * ``MILESTONE`` — ``name`` (fires subscribed ``OnMilestone`` activations).
    """

    kind: EffectKind
    target: str = ""
    node_type: str = ""
    edge_type: str = ""
    src: str = ""
    dst: str = ""
    attr: str = ""
    value: Any = None
    name: str = ""
    props: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, node_id: str, node_type: str, props: Mapping[str, Any] | None = None) -> Effect:
        """A ``CREATE_NODE`` effect adding ``node_id`` of ``node_type``."""
        return cls(EffectKind.CREATE_NODE, target=node_id, node_type=node_type, props=props or {})

    @classmethod
    def mutate(cls, node_id: str, attr: str, value: Any) -> Effect:
        """A ``MUTATE`` effect setting ``node_id.props[attr] = value``."""
        return cls(EffectKind.MUTATE, target=node_id, attr=attr, value=value)

    @classmethod
    def add_edge(cls, edge_id: str, edge_type: str, src: str, dst: str) -> Effect:
        """An ``ADD_EDGE`` effect adding the reified edge ``src -> dst``."""
        return cls(EffectKind.ADD_EDGE, target=edge_id, edge_type=edge_type, src=src, dst=dst)

    @classmethod
    def milestone(cls, name: str) -> Effect:
        """A ``MILESTONE`` effect announcing ``name`` was reached."""
        return cls(EffectKind.MILESTONE, name=name)


# --------------------------------------------------------------------------- #
# Conditions (the predicate an OnCondition watches — effect-driven re-eval).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Condition:
    """A KG-state predicate: ``world.node(node_id).props[attr] <op> value``.

    The scheduler subscribes each :class:`OnCondition` to the *attribute names* a
    condition reads (:meth:`watched_attrs`); when a ``MUTATE`` effect touches one,
    only conditions that read it are re-evaluated (the reactive machinery of
    §15.1), and a coarse daily tick covers the rest. A predicate over a missing
    node/attr is ``False``.
    """

    node_id: str
    attr: str
    op: FilterOp
    value: Any

    def watched_attrs(self) -> frozenset[str]:
        """Attribute names this condition reads (its effect subscriptions)."""
        return frozenset({self.attr})

    def evaluate(self, world: World) -> bool:
        """Return whether the predicate currently holds in ``world``."""
        node = world.get_node(self.node_id)
        if node is None:
            return False
        actual = node.props.get(self.attr)
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
        raise ValueError(f"unknown condition op: {self.op!r}")


# --------------------------------------------------------------------------- #
# The six triggers (ARCHITECTURE §12.1).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OnStart:
    """Fires once when the scenario begins (seeded at the window start)."""


@dataclass(frozen=True, slots=True)
class OnCadence:
    """Fires on a recurring schedule (``weekly:FRI``, ``daily:workdays``, ``every:2w``).

    All firings in the window are computed up front and seeded onto the queue
    (§15.1), so a cadence is deterministic and never depends on run order.
    """

    rule: str


@dataclass(frozen=True, slots=True)
class EventPredicate:
    """An optional ``where`` filter on a matched event (beyond its type).

    ``field`` is one of ``type`` / ``project`` / ``initiative``, or ``payload.KEY``
    to read ``event.payload[KEY]``. Same operators as a resolver
    :class:`~enterprise_sim.core.sim.resolver.Filter`.
    """

    field: str
    op: FilterOp
    value: Any

    def matches(self, event_fields: Mapping[str, Any]) -> bool:
        """Return whether ``event_fields`` (a flattened event view) satisfies this."""
        actual = event_fields.get(self.field)
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
        raise ValueError(f"unknown predicate op: {self.op!r}")


@dataclass(frozen=True, slots=True)
class OnEvent:
    """Reactive: fires when another process emits an event matching ``event_type``.

    The optional ``where`` predicates further constrain the match — this is how
    gates and cascades wire one process's output to another's input (§15.1).
    """

    event_type: str
    where: tuple[EventPredicate, ...] = ()


@dataclass(frozen=True, slots=True)
class OnMilestone:
    """Fires when a ``MILESTONE`` effect announces ``name`` (a project milestone)."""

    name: str


@dataclass(frozen=True, slots=True)
class OnCondition:
    """Fires when a KG-state predicate becomes true (effect-driven + daily tick)."""

    condition: Condition


@dataclass(frozen=True, slots=True)
class Probabilistic:
    """Seeded stochastic firing: ``rate`` arrivals per ``per`` period over the window.

    Pre-sampled as a seeded Poisson process (exponential inter-arrival), so the
    firings are fixed once the seed is — never sampled mid-run (§15.1).
    ``per`` is one of ``day`` / ``week`` / ``sprint`` / ``month``.
    """

    rate: float
    per: Literal["day", "week", "sprint", "month"] = "week"


Trigger = OnStart | OnCadence | OnEvent | OnMilestone | OnCondition | Probabilistic


# --------------------------------------------------------------------------- #
# Process structure (roles, steps, spread) and activations.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """A role a process binds: either resolved from the KG or bound on activation.

    Attributes:
        name: Role name referenced by steps' ``by`` and spreads' ``role``.
        selector: How to resolve the role from the KG (the resolver runs it). When
            ``None``, the role must be supplied via :attr:`Activation.bind`.
        relationship: Edge type the resolver writes from each pick to the
            activation's ``anchor`` (e.g. ``"reviews_for"``). ``None`` writes none.
    """

    name: str
    selector: Selector | None = None
    relationship: str | None = None


@dataclass(frozen=True, slots=True)
class Spread:
    """Distributes per-actor sub-events (comments) across a spanning step's window.

    Realises the multi-day comment threading of §15.2: each actor in :attr:`role`
    posts ``per_actor`` comments, seeded and spread across the step's working-time
    window around their busy slots, each threaded via ``in_reply_to`` to an
    already-emitted event (the draft or an earlier comment) so threads are always
    well-formed (conformance I4).

    Attributes:
        role: Whose actors post (e.g. the ``reviewers`` role).
        per_actor: How many comments each actor posts — ``int`` or ``"lo..hi"``.
        emits: Event type for each comment (default ``CommentPosted``).
        reply_rate: Probability a comment replies to an earlier comment rather
            than the step's parent event (``0`` ⇒ a flat fan-out off the parent).
    """

    role: str
    per_actor: int | str = "1..3"
    emits: str = "CommentPosted"
    reply_rate: float = 0.5


@dataclass(frozen=True, slots=True)
class Step:
    """One timed unit of a process: place it, emit an event, apply effects.

    Timing is resolved against the calendar (working time, D27): a step starts at
    ``at`` business-days after the instance, or ``after`` another step's end, plus
    an optional ``offset``; its acting role's slot is then placed greedily into the
    busy map (non-overlap). ``duration`` is the working-time window used both to
    chain ``after`` dependents and to spread :attr:`spread` comments over.

    Attributes:
        id: Step id, unique within its process.
        emits: Event type this step emits.
        by: Role whose people act in (and are booked for) this step.
        at: ``"day N"`` offset from the instance start (mutually exclusive with
            ``after``); defaults to ``"day 0"``.
        after: Id of a step this one follows; its window starts at that step's end.
        offset: Extra working-time after the ``at``/``after`` anchor (e.g. ``"1d"``).
        duration: Working-time length of the step's window (``"3d"``); ``None`` ⇒
            an instantaneous point event.
        slot: Working-time the actor is booked for in the busy map (default 30m);
            kept small for point events so a step never blocks a whole day.
        produces: Abstract deliverable requested, if any.
        effects: KG mutations applied when the event is emitted.
        spread: Multi-actor comment distribution over the step window, if any.
        parent_step: Id of the step whose event threads this one (causal parent);
            defaults to ``after`` when unset.
        payload: Static payload merged into the emitted event's brief.
    """

    id: str
    emits: str
    by: str | None = None
    at: str | None = None
    after: str | None = None
    offset: str | None = None
    duration: str | None = None
    slot: str | None = None
    produces: Deliverable | None = None
    effects: tuple[Effect, ...] = ()
    spread: Spread | None = None
    parent_step: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Process:
    """A reusable, named work activity: ordered steps over bound roles (§12.1).

    Attributes:
        name: Process name (also the seed sub-stream key and event-id namespace).
        roles: Roles the process binds (resolved or activation-supplied).
        steps: The timed steps, executed in declaration order within an instance.
        priority: Tie-break priority on the schedule key — **lower fires first**
            when two items share a timestamp (D26).
    """

    name: str
    roles: tuple[RoleSpec, ...] = ()
    steps: tuple[Step, ...] = ()
    priority: int = 100


@dataclass(frozen=True, slots=True)
class Activation:
    """Wires a process to a trigger, a role binding, and a focal anchor (§12.2).

    Attributes:
        id: Unique activation id (seed key + reactive-instance namespace).
        process: The process this activation instantiates on each firing.
        trigger: What causes it to fire (one of the six triggers).
        bind: Role → fixed node ids for roles not resolved from the KG (e.g. the
            author, or the project the work is about).
        anchor: Focal node id the work is *for* — the resolver's affinity anchor
            and edge endpoint, and a default subject on emitted events.
        params: Static parameters merged into every emitted event's payload.
    """

    id: str
    process: Process
    trigger: Trigger
    bind: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    anchor: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named set of activations forming the event-driven triggering graph (§12.1)."""

    name: str
    activations: tuple[Activation, ...] = ()
