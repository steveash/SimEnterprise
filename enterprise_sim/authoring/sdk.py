"""The declarative authoring SDK — what an author *writes* (ARCHITECTURE §12).

Decision D21: the authoring substrate is **declarative Python**, a builder/
dataclass surface, because selectors, triggers, and guards are *expressions* and
plugins are Python anyway. This module is that surface: the full §12.2 building
blocks — :class:`Selector` / :class:`Match`, :class:`Role`, :class:`EmittedEvent`,
:class:`KGEffect`, :class:`ConditionExpr`, :class:`Spread`, :class:`Step`,
:class:`Declares`, :class:`Process`, :class:`Activation`, :class:`Playbook` — and
the **six triggers** (:class:`OnStart`, :class:`OnCadence`, :class:`OnEvent`,
:class:`OnMilestone`, :class:`OnCondition`, :class:`Probabilistic`).

**Authoring layer vs. engine layer.** This is deliberately *distinct* from the
engine-side :mod:`enterprise_sim.core.sim.spec`. That module is the minimal typed
contract the scheduler *runs*; this one is the ergonomic, author-facing API that
later *produces* those engine objects (the "SDK lowers to the spec" boundary the
spec module's docstring describes). Keeping them separate lets the engine evolve
its internal representation without breaking authored playbooks, and lets the SDK
add author conveniences (descriptions, ``declares``, goal templates, the ``impl``
hatch, multi-event steps) the engine never needs to see. The one type shared
across the layer boundary is :class:`~enterprise_sim.core.events.Deliverable`,
the format-agnostic deliverable request that is already a cross-layer contract.

**Round-trip is part of the contract.** Every authoring object serializes to a
JSON-friendly mapping via ``to_dict`` and reconstructs via ``from_dict`` such that
``from_dict(x.to_dict()) == x`` (the acceptance criterion of esim-3c9cfc55).
This is what lets authored playbooks be stored, diffed, transported to the
linter/test-kit, and projected to/from the optional YAML form (§12).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from enterprise_sim.core.events import Deliverable

__all__ = [
    "Activation",
    "ConditionExpr",
    "Declares",
    "Deliverable",
    "EffectKind",
    "EmittedEvent",
    "KGEffect",
    "Match",
    "MatchOp",
    "OnCadence",
    "OnCondition",
    "OnEvent",
    "OnMilestone",
    "OnStart",
    "Playbook",
    "Probabilistic",
    "Process",
    "Role",
    "Selector",
    "Spread",
    "Step",
    "Trigger",
    "trigger_from_dict",
]

#: The comparison operators a :class:`Match` / :class:`ConditionExpr` may use.
#: Mirrors the engine resolver's ``FilterOp`` without importing it, so the
#: authoring layer carries no dependency on engine internals.
MatchOp = Literal["eq", "ne", "in", "contains", "gte", "lte"]


# --------------------------------------------------------------------------- #
# Selectors & roles — binding entities out of the knowledge graph.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Match:
    """One declarative predicate in a :class:`Selector` or :class:`OnEvent` ``where``.

    ``field`` names a node attribute (``type``) or a key in its props (``team``,
    ``seniority``, ``expertise`` …); ``op`` is one of :data:`MatchOp`. The
    semantics mirror the engine resolver's ``Filter`` exactly so a lowering pass
    is a field-for-field copy.
    """

    field: str
    op: MatchOp
    value: Any

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this predicate."""
        return {"field": self.field, "op": self.op, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Match:
        """Reconstruct a :class:`Match` from :meth:`to_dict` output."""
        return cls(field=data["field"], op=data["op"], value=data["value"])


@dataclass(frozen=True, slots=True)
class Selector:
    """Bind entities out of the KG: query by ``type``, filter, rank, draw ``count``.

    The §12.2 primitive ``Selector(type, where, exclude, rank_by, count)``, plus
    ``expertise`` (tags fed to the expertise ranking signal), ``distinct`` (no
    repeat picks), and ``external`` — the flag that marks an **out-of-org party**
    (a supplier, CRO, IRB) so the world builder knows to materialise it rather
    than expecting it among employees (§12.3 "external parties are
    ``Selector(external=...)``").

    Attributes:
        type: Node type to query (``"Person"``, ``"Supplier"`` …).
        where: Conjunction of :class:`Match` predicates candidates must satisfy.
        exclude: Node ids to drop (e.g. the author, to avoid self-review).
        rank_by: Ranking signals to combine (``"affinity"`` / ``"inverse_load"``
            / ``"expertise"``); empty means "rank by all configured signals".
        expertise: Required expertise tags for the ``expertise`` signal.
        count: How many to pick — a fixed ``int`` or a ``"lo..hi"`` range string.
        distinct: When ``True`` (default), never pick the same node twice.
        external: When ``True``, this binds an out-of-org party (§12.3).
    """

    type: str
    where: tuple[Match, ...] = ()
    exclude: tuple[str, ...] = ()
    rank_by: tuple[str, ...] = ()
    expertise: tuple[str, ...] = ()
    count: int | str = 1
    distinct: bool = True
    external: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this selector."""
        return {
            "type": self.type,
            "where": [m.to_dict() for m in self.where],
            "exclude": list(self.exclude),
            "rank_by": list(self.rank_by),
            "expertise": list(self.expertise),
            "count": self.count,
            "distinct": self.distinct,
            "external": self.external,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Selector:
        """Reconstruct a :class:`Selector` from :meth:`to_dict` output."""
        return cls(
            type=data["type"],
            where=tuple(Match.from_dict(m) for m in data.get("where", ())),
            exclude=tuple(data.get("exclude", ())),
            rank_by=tuple(data.get("rank_by", ())),
            expertise=tuple(data.get("expertise", ())),
            count=data.get("count", 1),
            distinct=data.get("distinct", True),
            external=data.get("external", False),
        )


@dataclass(frozen=True, slots=True)
class Role:
    """A named role a process or playbook binds (§12.2 ``Role(name, select=...)``).

    A role with a :attr:`select` is resolved from the KG at run time; a role with
    ``select=None`` is supplied on activation via :attr:`Activation.bind` (e.g. a
    fixed author or the project the work is about).

    Attributes:
        name: Role name referenced by steps' ``by`` and spreads' ``role``.
        select: The :class:`Selector` that resolves it, or ``None`` if bound.
        description: Optional human note (surfaces in the skill's pattern library).
    """

    name: str
    select: Selector | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this role."""
        return {
            "name": self.name,
            "select": self.select.to_dict() if self.select is not None else None,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Role:
        """Reconstruct a :class:`Role` from :meth:`to_dict` output."""
        select = data.get("select")
        return cls(
            name=data["name"],
            select=Selector.from_dict(select) if select is not None else None,
            description=data.get("description", ""),
        )


# --------------------------------------------------------------------------- #
# Emitted events, deliverables, KG effects.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmittedEvent:
    """A business event a :class:`Step` emits (§12.2 ``emits=[Event(...)]``).

    This is the *declaration* of an emitted event — its ``type`` plus a static
    ``payload`` brief merged into the runtime event — not the runtime
    :class:`~enterprise_sim.core.events.Event` (which also carries an id,
    timestamp, and resolved actors). Named ``EmittedEvent`` to keep that
    distinction unambiguous.
    """

    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this emitted-event spec."""
        return {"type": self.type, "payload": dict(self.payload)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EmittedEvent:
        """Reconstruct an :class:`EmittedEvent` from :meth:`to_dict` output."""
        return cls(type=data["type"], payload=dict(data.get("payload") or {}))


class EffectKind(StrEnum):
    """The kinds of KG mutation a step can declare (mirrors engine ``EffectKind``)."""

    CREATE_NODE = "create_node"
    MUTATE = "mutate"
    ADD_EDGE = "add_edge"
    MILESTONE = "milestone"


@dataclass(frozen=True, slots=True)
class KGEffect:
    """A declarative KG mutation a step applies (§12.2 ``effects=[...]``).

    Construct via the classmethods rather than the raw fields; which fields are
    meaningful depends on :attr:`kind` (see each classmethod). The field set
    mirrors the engine's ``Effect`` so lowering is a direct copy, and a
    ``MILESTONE`` effect is what fires a subscribed :class:`OnMilestone`.
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
    def create(
        cls, node_id: str, node_type: str, props: Mapping[str, Any] | None = None
    ) -> KGEffect:
        """A ``CREATE_NODE`` effect adding ``node_id`` of ``node_type``."""
        return cls(EffectKind.CREATE_NODE, target=node_id, node_type=node_type, props=props or {})

    @classmethod
    def mutate(cls, node_id: str, attr: str, value: Any) -> KGEffect:
        """A ``MUTATE`` effect setting ``node_id.props[attr] = value``.

        An :class:`OnCondition` whose predicate reads ``attr`` re-evaluates the
        moment this lands (the effect-driven path of §15.1).
        """
        return cls(EffectKind.MUTATE, target=node_id, attr=attr, value=value)

    @classmethod
    def add_edge(cls, edge_id: str, edge_type: str, src: str, dst: str) -> KGEffect:
        """An ``ADD_EDGE`` effect adding the reified edge ``src -> dst``."""
        return cls(EffectKind.ADD_EDGE, target=edge_id, edge_type=edge_type, src=src, dst=dst)

    @classmethod
    def milestone(cls, name: str) -> KGEffect:
        """A ``MILESTONE`` effect announcing ``name`` was reached."""
        return cls(EffectKind.MILESTONE, name=name)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this effect (all fields)."""
        return {
            "kind": self.kind.value,
            "target": self.target,
            "node_type": self.node_type,
            "edge_type": self.edge_type,
            "src": self.src,
            "dst": self.dst,
            "attr": self.attr,
            "value": self.value,
            "name": self.name,
            "props": dict(self.props),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> KGEffect:
        """Reconstruct a :class:`KGEffect` from :meth:`to_dict` output."""
        return cls(
            kind=EffectKind(data["kind"]),
            target=data.get("target", ""),
            node_type=data.get("node_type", ""),
            edge_type=data.get("edge_type", ""),
            src=data.get("src", ""),
            dst=data.get("dst", ""),
            attr=data.get("attr", ""),
            value=data.get("value"),
            name=data.get("name", ""),
            props=dict(data.get("props") or {}),
        )


@dataclass(frozen=True, slots=True)
class ConditionExpr:
    """A KG-state predicate: ``node.props[attr] <op> value`` (§12.1 ``OnCondition``).

    Also reusable as a :class:`Step` guard (``when=``). Structured rather than a
    free-text expression so it lints and lowers cleanly to the engine's
    ``Condition``; a missing node/attr evaluates ``False`` at run time.
    """

    node: str
    attr: str
    op: MatchOp
    value: Any

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this predicate."""
        return {"node": self.node, "attr": self.attr, "op": self.op, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConditionExpr:
        """Reconstruct a :class:`ConditionExpr` from :meth:`to_dict` output."""
        return cls(node=data["node"], attr=data["attr"], op=data["op"], value=data["value"])


# --------------------------------------------------------------------------- #
# The six triggers (ARCHITECTURE §12.1). Each round-trips via a ``trigger`` tag.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OnStart:
    """Fires once when the scenario begins (§12.1)."""

    def to_dict(self) -> dict[str, Any]:
        """Return the tagged mapping for this trigger."""
        return {"trigger": "on_start"}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OnStart:
        """Reconstruct from :meth:`to_dict` output."""
        return cls()


@dataclass(frozen=True, slots=True)
class OnCadence:
    """Fires on a recurring schedule — ``weekly:FRI``, ``per_sprint:2w``, ``daily:workdays``."""

    rule: str

    def to_dict(self) -> dict[str, Any]:
        """Return the tagged mapping for this trigger."""
        return {"trigger": "on_cadence", "rule": self.rule}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OnCadence:
        """Reconstruct from :meth:`to_dict` output."""
        return cls(rule=data["rule"])


@dataclass(frozen=True, slots=True)
class OnEvent:
    """Reactive: fires when another process emits an event matching ``type``.

    The optional ``where`` :class:`Match` predicates further constrain the match
    — this is how gates and cascades wire one process's output to another's input.
    """

    type: str
    where: tuple[Match, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the tagged mapping for this trigger."""
        return {
            "trigger": "on_event",
            "type": self.type,
            "where": [m.to_dict() for m in self.where],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OnEvent:
        """Reconstruct from :meth:`to_dict` output."""
        return cls(
            type=data["type"],
            where=tuple(Match.from_dict(m) for m in data.get("where", ())),
        )


@dataclass(frozen=True, slots=True)
class OnMilestone:
    """Fires when a ``MILESTONE`` effect announces ``name`` (a project milestone)."""

    name: str

    def to_dict(self) -> dict[str, Any]:
        """Return the tagged mapping for this trigger."""
        return {"trigger": "on_milestone", "name": self.name}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OnMilestone:
        """Reconstruct from :meth:`to_dict` output."""
        return cls(name=data["name"])


@dataclass(frozen=True, slots=True)
class OnCondition:
    """Fires when a KG-state predicate becomes true (effect-driven + daily tick)."""

    expr: ConditionExpr

    def to_dict(self) -> dict[str, Any]:
        """Return the tagged mapping for this trigger."""
        return {"trigger": "on_condition", "expr": self.expr.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OnCondition:
        """Reconstruct from :meth:`to_dict` output."""
        return cls(expr=ConditionExpr.from_dict(data["expr"]))


@dataclass(frozen=True, slots=True)
class Probabilistic:
    """Seeded stochastic firing: ``rate`` arrivals per ``per`` period over the window.

    Pre-sampled from the seeded RNG, so firings are fixed once the seed is. ``per``
    is one of ``day`` / ``week`` / ``sprint`` / ``month``.
    """

    rate: float
    per: Literal["day", "week", "sprint", "month"] = "week"

    def to_dict(self) -> dict[str, Any]:
        """Return the tagged mapping for this trigger."""
        return {"trigger": "probabilistic", "rate": self.rate, "per": self.per}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Probabilistic:
        """Reconstruct from :meth:`to_dict` output."""
        return cls(rate=data["rate"], per=data.get("per", "week"))


Trigger = OnStart | OnCadence | OnEvent | OnMilestone | OnCondition | Probabilistic

#: Dispatch table from a serialized ``trigger`` tag to its class.
_TRIGGERS: dict[str, type[Trigger]] = {
    "on_start": OnStart,
    "on_cadence": OnCadence,
    "on_event": OnEvent,
    "on_milestone": OnMilestone,
    "on_condition": OnCondition,
    "probabilistic": Probabilistic,
}


def trigger_from_dict(data: Mapping[str, Any]) -> Trigger:
    """Reconstruct one of the six triggers from its tagged :meth:`to_dict` output."""
    tag = data["trigger"]
    try:
        cls = _TRIGGERS[tag]
    except KeyError as exc:
        raise ValueError(f"unknown trigger tag: {tag!r}") from exc
    return cls.from_dict(data)


# --------------------------------------------------------------------------- #
# Steps (with multi-actor spread) and processes.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Spread:
    """Distribute per-actor sub-events (comments) across a step's window (§12.2 ``repeat=``).

    Each actor in :attr:`role` posts :attr:`per_actor` events of type
    :attr:`emits`, spread (seeded) over the spanning step's ``over`` window and
    threaded back to an earlier event so threads stay well-formed.

    Attributes:
        role: Whose actors post (e.g. the ``reviewers`` role).
        per_actor: How many each posts — ``int`` or ``"lo..hi"`` range string.
        over: Which window to spread across (typically ``"duration"``).
        emits: Event type for each sub-event (default ``CommentPosted``).
        reply_rate: Probability a post replies to an earlier post vs. the parent.
    """

    role: str
    per_actor: int | str = "1..3"
    over: str = "duration"
    emits: str = "CommentPosted"
    reply_rate: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this spread."""
        return {
            "role": self.role,
            "per_actor": self.per_actor,
            "over": self.over,
            "emits": self.emits,
            "reply_rate": self.reply_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Spread:
        """Reconstruct a :class:`Spread` from :meth:`to_dict` output."""
        return cls(
            role=data["role"],
            per_actor=data.get("per_actor", "1..3"),
            over=data.get("over", "duration"),
            emits=data.get("emits", "CommentPosted"),
            reply_rate=data.get("reply_rate", 0.5),
        )


@dataclass(frozen=True, slots=True)
class Step:
    """One timed unit of a :class:`Process`: place it, emit events, apply effects.

    Timing resolves against the working calendar (D27): a step starts ``at`` a
    ``"day N"`` offset from the instance, or ``after`` another step's end, plus an
    optional ``offset``; ``duration`` is its working-time window. ``when`` guards
    the step (skipped if the predicate is false); ``repeat`` spreads multi-actor
    sub-events over the window.

    Attributes:
        id: Step id, unique within its process.
        by: Role whose people act in (and are booked for) this step.
        at: ``"day N"`` offset from the instance start (mutually exclusive with
            ``after``); defaults to ``"day 0"`` when both are unset.
        after: Id of a step this one follows; its window starts at that step's end.
        offset: Extra working-time after the ``at``/``after`` anchor (``"1d"``).
        duration: Working-time length of the step's window (``"3d"``); ``None`` ⇒
            an instantaneous point event.
        slot: Working-time the actor is booked for (default left to the engine).
        emits: The event(s) this step emits (the §12.2 ``emits=[Event(...)]`` list).
        produces: Abstract deliverable requested, if any.
        effects: KG mutations applied when the step's event is emitted.
        repeat: Multi-actor sub-event distribution over the window, if any.
        when: Guard predicate; the step is skipped unless it holds.
        parent_step: Id of the step whose event threads this one (causal parent).
    """

    id: str
    by: str | None = None
    at: str | None = None
    after: str | None = None
    offset: str | None = None
    duration: str | None = None
    slot: str | None = None
    emits: tuple[EmittedEvent, ...] = ()
    produces: Deliverable | None = None
    effects: tuple[KGEffect, ...] = ()
    repeat: Spread | None = None
    when: ConditionExpr | None = None
    parent_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this step."""
        return {
            "id": self.id,
            "by": self.by,
            "at": self.at,
            "after": self.after,
            "offset": self.offset,
            "duration": self.duration,
            "slot": self.slot,
            "emits": [e.to_dict() for e in self.emits],
            "produces": self.produces.to_dict() if self.produces is not None else None,
            "effects": [e.to_dict() for e in self.effects],
            "repeat": self.repeat.to_dict() if self.repeat is not None else None,
            "when": self.when.to_dict() if self.when is not None else None,
            "parent_step": self.parent_step,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Step:
        """Reconstruct a :class:`Step` from :meth:`to_dict` output."""
        produces = data.get("produces")
        repeat = data.get("repeat")
        when = data.get("when")
        return cls(
            id=data["id"],
            by=data.get("by"),
            at=data.get("at"),
            after=data.get("after"),
            offset=data.get("offset"),
            duration=data.get("duration"),
            slot=data.get("slot"),
            emits=tuple(EmittedEvent.from_dict(e) for e in data.get("emits", ())),
            produces=Deliverable.from_dict(produces) if produces is not None else None,
            effects=tuple(KGEffect.from_dict(e) for e in data.get("effects", ())),
            repeat=Spread.from_dict(repeat) if repeat is not None else None,
            when=ConditionExpr.from_dict(when) if when is not None else None,
            parent_step=data.get("parent_step"),
        )


@dataclass(frozen=True, slots=True)
class Declares:
    """The trusted summary the engine reads instead of executing a process (§12.1).

    A process declares the event types it emits, the deliverable kinds it
    produces, and the effects it applies; the engine trusts this block for graph
    soundness and binding, and the test kit dynamically checks the real run
    against it (conformance I5) — critical for ``impl``-backed processes.
    """

    events: tuple[str, ...] = ()
    deliverables: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this declares block."""
        return {
            "events": list(self.events),
            "deliverables": list(self.deliverables),
            "effects": list(self.effects),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Declares:
        """Reconstruct a :class:`Declares` from :meth:`to_dict` output."""
        return cls(
            events=tuple(data.get("events", ())),
            deliverables=tuple(data.get("deliverables", ())),
            effects=tuple(data.get("effects", ())),
        )


@dataclass(frozen=True, slots=True)
class Process:
    """A reusable, named work activity (§12.1): timed steps over bound roles.

    A process is either **declarative** (a list of :attr:`steps`) or backed by a
    code **escape hatch** (:attr:`impl`, a ``"pkg.module:ClassName"`` ref) for
    logic the steps can't express — e.g. a stateful purchase-order lifecycle. In
    both cases :attr:`declares` is the contract the engine trusts.

    Attributes:
        name: Process name (also the event-id namespace and seed sub-stream key).
        description: Human description (surfaces in the pattern library, §14).
        roles: Roles the process binds (resolved selectors or activation-bound).
        params: Static parameters merged into every emitted event's payload.
        steps: The timed steps, in declaration order (empty when ``impl`` is set).
        impl: ``"pkg.module:ClassName"`` escape hatch, or ``None`` for declarative.
        declares: The trusted events/deliverables/effects summary.
        priority: Schedule tie-break — **lower fires first** at equal timestamps.
    """

    name: str
    description: str = ""
    roles: tuple[Role, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple[Step, ...] = ()
    impl: str | None = None
    declares: Declares = field(default_factory=Declares)
    priority: int = 100

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this process."""
        return {
            "name": self.name,
            "description": self.description,
            "roles": [r.to_dict() for r in self.roles],
            "params": dict(self.params),
            "steps": [s.to_dict() for s in self.steps],
            "impl": self.impl,
            "declares": self.declares.to_dict(),
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Process:
        """Reconstruct a :class:`Process` from :meth:`to_dict` output."""
        declares = data.get("declares")
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            roles=tuple(Role.from_dict(r) for r in data.get("roles", ())),
            params=dict(data.get("params") or {}),
            steps=tuple(Step.from_dict(s) for s in data.get("steps", ())),
            impl=data.get("impl"),
            declares=Declares.from_dict(declares) if declares is not None else Declares(),
            priority=data.get("priority", 100),
        )


# --------------------------------------------------------------------------- #
# Activations and playbooks — the event-driven composition.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Activation:
    """Wires a process to a trigger, a role binding, and a focal anchor (§12.2).

    Attributes:
        id: Unique activation id within the playbook.
        process: The process this activation instantiates on each firing.
        trigger: What causes it to fire (one of the six triggers).
        bind: Role → fixed node ids for roles not resolved from the KG.
        anchor: Focal node the work is *for* (affinity anchor, edge endpoint, and
            a default subject on emitted events).
        params: Static parameters merged into every emitted event's payload.
    """

    id: str
    process: Process
    trigger: Trigger
    bind: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    anchor: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this activation."""
        return {
            "id": self.id,
            "process": self.process.to_dict(),
            "trigger": self.trigger.to_dict(),
            "bind": {role: list(ids) for role, ids in self.bind.items()},
            "anchor": self.anchor,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Activation:
        """Reconstruct an :class:`Activation` from :meth:`to_dict` output."""
        bind_raw: Mapping[str, Sequence[str]] = data.get("bind") or {}
        return cls(
            id=data["id"],
            process=Process.from_dict(data["process"]),
            trigger=trigger_from_dict(data["trigger"]),
            bind={role: tuple(ids) for role, ids in bind_raw.items()},
            anchor=data.get("anchor"),
            params=dict(data.get("params") or {}),
        )


@dataclass(frozen=True, slots=True)
class Playbook:
    """A goal-oriented composition (§12.1): scenario roles + an activation graph.

    The activations form an event-driven triggering graph — one process's emitted
    event can trigger another's :class:`OnEvent` — across the six triggers.
    :attr:`deliverable_expectations` is the author's claim about what artifact
    kinds the playbook should yield, checked by the test kit (invariant P4).

    Attributes:
        name: Playbook name (e.g. ``build_software``).
        vertical: Business vertical (``technology``, ``retail``, ``pharma`` …).
        goal_template: A short templated statement of the playbook's goal.
        roles: Scenario-level roles shared across activations.
        activations: The wired activations forming the triggering graph.
        deliverable_expectations: Deliverable kinds the playbook is expected to emit.
    """

    name: str
    vertical: str
    goal_template: str = ""
    roles: tuple[Role, ...] = ()
    activations: tuple[Activation, ...] = ()
    deliverable_expectations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this playbook (the whole tree)."""
        return {
            "name": self.name,
            "vertical": self.vertical,
            "goal_template": self.goal_template,
            "roles": [r.to_dict() for r in self.roles],
            "activations": [a.to_dict() for a in self.activations],
            "deliverable_expectations": list(self.deliverable_expectations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Playbook:
        """Reconstruct a :class:`Playbook` from :meth:`to_dict` output."""
        return cls(
            name=data["name"],
            vertical=data["vertical"],
            goal_template=data.get("goal_template", ""),
            roles=tuple(Role.from_dict(r) for r in data.get("roles", ())),
            activations=tuple(Activation.from_dict(a) for a in data.get("activations", ())),
            deliverable_expectations=tuple(data.get("deliverable_expectations", ())),
        )
