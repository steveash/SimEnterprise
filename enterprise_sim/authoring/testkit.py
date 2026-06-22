"""Tier-2 isolated test kit + built-in conformance suite (ARCHITECTURE §13, D23).

This is the second validation tier (§13): **deterministic execution in isolation**.
An author writes a process or playbook with the SDK and runs it here against an
**auto-synthesised world** — no hand-built fixtures — to get back a frozen event
stream they can assert against, plus a **conformance suite that runs for free on
every process** (the author writes none). The three moving parts:

* :class:`TestWorld` — ``TestWorld.satisfying(process)`` reads a process's roles and
  generates a minimal KG that binds them (§13 "auto-synthesised world"): a candidate
  pool per selector, a bound node per activation-bound role, the activation anchor,
  and any node a step effect or condition references, so the run has real entities to
  resolve, mutate, and thread against.
* :func:`run_process` / :func:`run_playbook` — lower the SDK to the engine spec
  (:mod:`enterprise_sim.authoring.lowering`), drive the
  :class:`~enterprise_sim.core.sim.Scheduler` over a window, and return a
  :class:`RunResult` wrapping the ordered log with fluent queries
  (``result.events("CommentPosted").count``, ``result.deliverable("design_doc")``).
* :func:`check_conformance` (process invariants **I1–I8**) and
  :func:`check_playbook` (playbook invariants **P1–P6**) — the built-in suite, plus
  :func:`assert_conforms` which raises on any violation, and golden-snapshot helpers
  (:func:`snapshot`, :func:`assert_golden`) for regression.

Everything is deterministic by construction (D23 runs on the **fake** LLM backend's
world — the scheduler emits a fully-ordered, seeded log with no content rendering),
so the same process and seed always yield byte-identical streams; invariant I6
asserts exactly that by re-running from a fresh copy of the synthesised world.

**Scope.** The declarative engine has no ``impl`` runner yet, so an ``impl``-backed
process runs to an empty stream; Tier-2 execution and the dynamic conformance suite
therefore target **declarative** processes (the common case). The static playbook
suite (P1–P6) still covers ``impl`` processes through their ``declares`` block.
"""

from __future__ import annotations

import ast
import inspect
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from enterprise_sim.authoring import sdk
from enterprise_sim.authoring.lowering import lower_activation, lower_process
from enterprise_sim.core.events import Event, EventJournal
from enterprise_sim.core.sim import (
    Activation,
    OnStart,
    Scenario,
    Scheduler,
    ValidationIssue,
    WorkingCalendar,
)
from enterprise_sim.core.sim.resolver import Filter
from enterprise_sim.core.world import Node, World

__all__ = [
    "ConformanceViolation",
    "EventQuery",
    "RunResult",
    "TestWorld",
    "assert_conforms",
    "assert_golden",
    "check_conformance",
    "check_playbook",
    "run_playbook",
    "run_process",
    "scan_nondeterminism",
    "snapshot",
]

#: A Monday 09:00 in working time — the default window start for an isolated run.
DEFAULT_START = datetime(2026, 1, 5, 9, 0)
#: A twelve-week default window — generous headroom for cadence / probabilistic seeds.
DEFAULT_WINDOW = timedelta(days=84)
#: The default seed an isolated run uses (§13 ``run_process(..., seed=1)``).
DEFAULT_SEED = 1
#: Time every synthesised node is stamped at (before the window — entities pre-exist).
_SYNTH_TIME = datetime(2026, 1, 1, 9, 0)
#: Env var that flips :func:`assert_golden` into record mode.
_GOLDEN_UPDATE_ENV = "ESIM_UPDATE_GOLDEN"


# --------------------------------------------------------------------------- #
# Auto-synthesised world (§13 "TestWorld.satisfying(process)").
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TestWorld:
    """A minimal KG that binds a process/playbook, synthesised from its roles (§13).

    Built by :meth:`satisfying` (one process) or :meth:`for_playbook` (a triggering
    graph): for every role with a selector it generates a small candidate pool the
    resolver can draw from; for every activation-bound role and every effect/condition
    target it generates the referenced node; and it records the bindings and anchor a
    run needs so no hand-built fixture is required.

    Attributes:
        world: The synthesised knowledge graph.
        bindings: Role → node ids for activation-bound (selector-less) roles.
        anchor: The focal node id the work is *for* (the activation anchor).
    """

    world: World
    bindings: Mapping[str, tuple[str, ...]]
    anchor: str | None

    @classmethod
    def satisfying(cls, process: sdk.Process, *, anchor: str | None = None) -> TestWorld:
        """Synthesise a world that binds ``process`` in isolation (the §13 helper).

        Args:
            process: The process whose roles drive synthesis.
            anchor: Focal node the work is for; one is invented (``anchor:<name>``)
                when omitted.

        Returns:
            A :class:`TestWorld` ready for :func:`run_process`.
        """
        synth = _Synth()
        bindings = synth.add_roles(process.roles)
        resolved_anchor = anchor or f"anchor:{process.name}"
        synth.ensure_node(resolved_anchor, "Anchor")
        synth.add_effect_targets(process.steps)
        return cls(world=synth.world, bindings=bindings, anchor=resolved_anchor)

    @classmethod
    def for_playbook(cls, playbook: sdk.Playbook) -> TestWorld:
        """Synthesise a world binding every activation in ``playbook`` (the §13 helper).

        Unions the roles of every activated process plus the playbook-level roles,
        materialises each activation's anchor and explicit bindings, and pre-creates
        every node a step effect references — so the whole triggering graph has real
        entities to run against.
        """
        synth = _Synth()
        # Playbook- and process-level roles (selectors → candidate pools).
        synth.add_roles(playbook.roles)
        for act in playbook.activations:
            synth.add_roles(act.process.roles)
        # Anchors, explicit binds, and effect targets per activation.
        for act in playbook.activations:
            if act.anchor is not None:
                synth.ensure_node(act.anchor, "Anchor")
            for ids in act.bind.values():
                for node_id in ids:
                    synth.ensure_node(node_id, _guess_type(node_id))
            synth.add_effect_targets(act.process.steps)
        return cls(world=synth.world, bindings={}, anchor=None)


class _Synth:
    """Incremental world builder shared by :meth:`TestWorld.satisfying`/``for_playbook``."""

    def __init__(self) -> None:
        self.world = World()

    def ensure_node(
        self, node_id: str, node_type: str, props: Mapping[str, Any] | None = None
    ) -> None:
        """Add ``node_id`` of ``node_type`` if absent (idempotent, deterministic)."""
        if self.world.get_node(node_id) is None:
            self.world.add_node(Node(node_id, node_type, _SYNTH_TIME, props=dict(props or {})))

    def add_roles(self, roles: Iterable[sdk.Role]) -> dict[str, tuple[str, ...]]:
        """Synthesise candidates/bindings for ``roles``; return selector-less bindings."""
        bindings: dict[str, tuple[str, ...]] = {}
        for role in roles:
            if role.select is None:
                node_id = f"bound:{role.name}"
                self.ensure_node(node_id, "Person")
                bindings[role.name] = (node_id,)
            else:
                self._add_candidates(role)
        return bindings

    def _add_candidates(self, role: sdk.Role) -> None:
        """Generate a candidate pool satisfying ``role``'s selector (enough for ``count``)."""
        selector = role.select
        assert selector is not None
        _, hi = _count_range(selector.count)
        # A pool slightly larger than the draw so ranking/availability bias has room;
        # external parties are singletons (one supplier / IRB), so just one.
        n = 1 if selector.external else max(hi + 2, 2)
        props = _props_satisfying(selector)
        for i in range(n):
            node_id = f"{role.name}:{i}"
            if self.world.get_node(node_id) is None:
                self.world.add_node(Node(node_id, selector.type, _SYNTH_TIME, props=dict(props)))

    def add_effect_targets(self, steps: Iterable[sdk.Step]) -> None:
        """Pre-create nodes a step effect references but does not itself create (I7).

        ``CREATE_NODE`` targets come into being at run time; ``MUTATE`` targets and
        ``ADD_EDGE`` endpoints must already exist for the effect to land on a real
        entity, so any not covered by a create are materialised here.
        """
        created: set[str] = set()
        for step in steps:
            for effect in step.effects:
                if effect.kind is sdk.EffectKind.CREATE_NODE:
                    created.add(effect.target)
        for step in steps:
            for effect in step.effects:
                if effect.kind is sdk.EffectKind.MUTATE and effect.target not in created:
                    self.ensure_node(effect.target, _guess_type(effect.target))
                elif effect.kind is sdk.EffectKind.ADD_EDGE:
                    for endpoint in (effect.src, effect.dst):
                        if endpoint and endpoint not in created:
                            self.ensure_node(endpoint, _guess_type(endpoint))


def _props_satisfying(selector: sdk.Selector) -> dict[str, Any]:
    """Build a props dict that satisfies every ``where`` filter + expertise of a selector."""
    props: dict[str, Any] = {}
    for match in selector.where:
        props[match.field] = _value_satisfying(match)
    if selector.expertise:
        existing = props.get("expertise")
        tags = list(existing) if isinstance(existing, (list, tuple)) else []
        for tag in selector.expertise:
            if tag not in tags:
                tags.append(tag)
        props["expertise"] = tags
    return props


def _value_satisfying(match: sdk.Match) -> Any:
    """A property value that makes ``match`` true (``id``/``type`` are not props)."""
    if match.op == "eq":
        return match.value
    if match.op in ("gte", "lte"):
        return match.value
    if match.op == "in":
        seq = match.value
        return seq[0] if isinstance(seq, (list, tuple)) and seq else match.value
    if match.op == "contains":
        return [match.value]
    if match.op == "ne":
        # A sentinel distinct from the excluded value.
        return f"not::{match.value}"
    return match.value


def _count_range(count: int | str) -> tuple[int, int]:
    """Parse a selector ``count`` (``2`` or ``"1..3"``) into ``(lo, hi)``."""
    if isinstance(count, int):
        return count, count
    text = count.strip()
    if ".." in text:
        lo_s, _, hi_s = text.partition("..")
        return int(lo_s), int(hi_s)
    return int(text), int(text)


def _guess_type(node_id: str) -> str:
    """Guess a node type from an id like ``study:trial7`` → ``Study`` (synthesis only)."""
    prefix = node_id.split(":", 1)[0] if ":" in node_id else node_id
    return prefix[:1].upper() + prefix[1:] if prefix else "Node"


# --------------------------------------------------------------------------- #
# Fluent result + queries.
# --------------------------------------------------------------------------- #


class EventQuery:
    """A fluent, chainable view over a list of events (``.count``, ``.where(...)``).

    Supports ``len()``/iteration and the §13 fluent-assertion shape
    ``result.events("CommentPosted").count`` and ``.where(payload_key=value)``.
    """

    def __init__(self, events: Sequence[Event]) -> None:
        self._events = list(events)

    @property
    def count(self) -> int:
        """How many events the query holds."""
        return len(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __bool__(self) -> bool:
        return bool(self._events)

    def __getitem__(self, index: int) -> Event:
        return self._events[index]

    def of_type(self, event_type: str) -> EventQuery:
        """Narrow to events of ``event_type``."""
        return EventQuery([e for e in self._events if e.type == event_type])

    def where(self, **payload_eq: Any) -> EventQuery:
        """Narrow to events whose ``payload`` matches every ``key=value`` given."""
        return EventQuery(
            [e for e in self._events if all(e.payload.get(k) == v for k, v in payload_eq.items())]
        )

    def actors(self, role: str | None = None) -> set[str]:
        """Union of actor ids across the query, optionally restricted to ``role``."""
        ids: set[str] = set()
        for event in self._events:
            if role is None:
                for people in event.actors.values():
                    ids.update(people)
            else:
                ids.update(event.actors.get(role, ()))
        return ids

    def first(self) -> Event | None:
        """The earliest event in the query (journal order), or ``None`` if empty."""
        return self._events[0] if self._events else None


@dataclass(frozen=True, slots=True)
class _Scope:
    """Per-process context the conformance suite needs to judge an event."""

    process: sdk.Process
    role_selectors: Mapping[str, sdk.Selector | None]
    bound: Mapping[str, tuple[str, ...]]


class RunResult:
    """The frozen outcome of an isolated run, with fluent queries and the run context.

    Wraps the scheduler's ordered :class:`~enterprise_sim.core.events.EventJournal`
    plus the mutated world, soft validation issues, and enough context (the SDK
    processes, role selectors/bindings, window, and a deterministic re-run thunk) for
    the conformance suite to judge it. Returned by both :func:`run_process` (one
    process) and :func:`run_playbook` (a triggering graph).
    """

    def __init__(
        self,
        *,
        journal: EventJournal,
        world: World,
        issues: tuple[ValidationIssue, ...],
        scopes: Sequence[_Scope],
        start: datetime,
        end: datetime,
        calendar: WorkingCalendar,
        rerun: Callable[[], EventJournal],
    ) -> None:
        self.journal = journal
        self.world = world
        self.issues = issues
        self.start = start
        self.end = end
        self.calendar = calendar
        self._scopes = tuple(scopes)
        self._rerun = rerun
        # Index each emitted / spread event type to the process that produces it.
        self._type_scope: dict[str, _Scope] = {}
        for scope in self._scopes:
            for step in scope.process.steps:
                for emitted in step.emits:
                    self._type_scope.setdefault(emitted.type, scope)
                if step.repeat is not None:
                    self._type_scope.setdefault(step.repeat.emits, scope)

    # -- fluent queries -----------------------------------------------------

    @property
    def all_events(self) -> EventQuery:
        """Every emitted event, in journal (insertion) order."""
        return EventQuery(list(self.journal))

    def events(self, event_type: str | None = None) -> EventQuery:
        """Events, optionally of a single ``event_type`` (the §13 ``.events(type)``)."""
        if event_type is None:
            return self.all_events
        return EventQuery([e for e in self.journal if e.type == event_type])

    def event_types(self) -> set[str]:
        """The distinct event types the run emitted."""
        return {e.type for e in self.journal}

    def deliverables(self, kind: str | None = None) -> EventQuery:
        """Events carrying a deliverable, optionally of a single ``kind``."""
        out = [e for e in self.journal if e.deliverable is not None]
        if kind is not None:
            out = [e for e in out if e.deliverable is not None and e.deliverable.kind == kind]
        return EventQuery(out)

    def deliverable(self, kind: str) -> Event | None:
        """The first event producing a deliverable of ``kind`` (``.deliverable(...)``)."""
        return self.deliverables(kind).first()

    def deliverable_kinds(self) -> set[str]:
        """The distinct deliverable kinds the run produced."""
        return {e.deliverable.kind for e in self.journal if e.deliverable is not None}

    def has_milestone(self, name: str) -> bool:
        """Whether any process applied a ``MILESTONE`` effect named ``name``."""
        for scope in self._scopes:
            for step in scope.process.steps:
                for effect in step.effects:
                    if effect.kind is sdk.EffectKind.MILESTONE and effect.name == name:
                        return True
        return False

    def snapshot(self) -> str:
        """The canonical, deterministic JSONL of the event stream (for golden tests)."""
        return self.journal.dumps()

    def rerun(self) -> EventJournal:
        """Re-execute the run from a fresh copy of the synthesised world (for I6)."""
        return self._rerun()


# --------------------------------------------------------------------------- #
# Running a process / playbook in isolation.
# --------------------------------------------------------------------------- #


def run_process(
    process: sdk.Process,
    world: World | None = None,
    *,
    start: datetime = DEFAULT_START,
    end: datetime | None = None,
    seed: int = DEFAULT_SEED,
    calendar: WorkingCalendar | None = None,
    anchor: str | None = None,
) -> RunResult:
    """Run ``process`` in isolation under a single ``OnStart`` and return the stream.

    The §13 ``run_process(p, world, start=…, seed=1)``: lower the SDK process to the
    engine spec, wrap it in a one-shot ``OnStart`` activation over an auto-synthesised
    world (unless one is supplied), and drive the scheduler across the window.

    Args:
        process: The SDK process to run.
        world: A pre-built KG; when ``None`` a :class:`TestWorld` is synthesised.
        start: Window start (a working-time Monday by default).
        end: Window end; ``start + 12 weeks`` by default.
        seed: Run root seed (default ``1`` — deterministic).
        calendar: Working calendar; a default Mon–Fri 9–17 one is used when omitted.
        anchor: Focal node id; invented during synthesis when omitted.

    Returns:
        A :class:`RunResult` over the ordered event log.
    """
    cal = calendar or WorkingCalendar()
    window_end = end or (start + DEFAULT_WINDOW)

    if world is None:
        tw = TestWorld.satisfying(process, anchor=anchor)
        base_world, bindings, resolved_anchor = tw.world, dict(tw.bindings), tw.anchor
    else:
        base_world, bindings, resolved_anchor = world, {}, anchor

    activation = Activation(
        id=f"{process.name}@test",
        process=lower_process(process),
        trigger=OnStart(),
        bind={role: tuple(ids) for role, ids in bindings.items()},
        anchor=resolved_anchor,
    )
    scenario = Scenario(name=f"test:{process.name}", activations=(activation,))
    scope = _Scope(
        process=process,
        role_selectors={r.name: r.select for r in process.roles},
        bound={role: tuple(ids) for role, ids in bindings.items()},
    )
    return _drive(scenario, base_world, (scope,), start, window_end, seed, cal)


def run_playbook(
    playbook: sdk.Playbook,
    world: World | None = None,
    *,
    start: datetime = DEFAULT_START,
    end: datetime | None = None,
    seed: int = DEFAULT_SEED,
    calendar: WorkingCalendar | None = None,
) -> RunResult:
    """Run a whole ``playbook`` (the triggering graph) in isolation (§13 ``run_playbook``).

    Lowers every activation, synthesises a world binding the union of their roles
    (unless one is supplied), and drives the scheduler so one process's emitted event
    can trigger another's ``OnEvent`` — exactly the engine's reactive path.
    """
    cal = calendar or WorkingCalendar()
    window_end = end or (start + DEFAULT_WINDOW)
    base_world = world if world is not None else TestWorld.for_playbook(playbook).world

    scenario = Scenario(
        name=playbook.name,
        activations=tuple(lower_activation(a) for a in playbook.activations),
    )
    # One scope per distinct process in the playbook (keyed by process name).
    scopes: dict[str, _Scope] = {}
    for act in playbook.activations:
        proc = act.process
        scopes.setdefault(
            proc.name,
            _Scope(
                process=proc,
                role_selectors={r.name: r.select for r in proc.roles},
                bound={role: tuple(ids) for role, ids in act.bind.items()},
            ),
        )
    return _drive(scenario, base_world, tuple(scopes.values()), start, window_end, seed, cal)


def _drive(
    scenario: Scenario,
    world: World,
    scopes: Sequence[_Scope],
    start: datetime,
    end: datetime,
    seed: int,
    calendar: WorkingCalendar,
) -> RunResult:
    """Snapshot the world, run the scheduler, and wrap a deterministic re-run thunk."""
    # Snapshot the pristine world *before* the scheduler mutates it, so the I6 re-run
    # starts from identical state regardless of how the world was built.
    pristine = world.to_json()

    def rerun() -> EventJournal:
        fresh = World.from_json(pristine)
        result = Scheduler(fresh, calendar, root_seed=seed).run(scenario, start=start, end=end)
        return result.journal

    result = Scheduler(world, calendar, root_seed=seed).run(scenario, start=start, end=end)
    return RunResult(
        journal=result.journal,
        world=result.world,
        issues=result.issues,
        scopes=scopes,
        start=start,
        end=end,
        calendar=calendar,
        rerun=rerun,
    )


# --------------------------------------------------------------------------- #
# Conformance suite — process invariants I1–I8 (§13).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConformanceViolation:
    """One failed invariant (the suite returns a list; empty ⇒ conformant).

    Attributes:
        code: The invariant id (``I1``..``I8`` / ``P1``..``P6``).
        message: Human-readable detail.
        subject: Related event/node id, if applicable.
    """

    code: str
    message: str
    subject: str | None = None

    def __str__(self) -> str:
        where = f" [{self.subject}]" if self.subject else ""
        return f"{self.code}: {self.message}{where}"


def check_conformance(result: RunResult) -> list[ConformanceViolation]:
    """Run the built-in process suite (I1–I8) over ``result`` (§13).

    Applied to *every* process for free — the author writes none. Returns the list of
    violations (empty means conformant). The invariants:

    * **I1** every event timestamp is within the window and in working hours;
    * **I2** the timeline is monotonic — a causal child never precedes its parent;
    * **I3** participants are real entities of a declared role (exclude/distinct held);
    * **I4** comment threads are well-formed (every reply resolves to an earlier event);
    * **I5** the run conforms to ``declares`` (no event/deliverable/effect beyond it);
    * **I6** determinism — a second seeded run emits an identical stream;
    * **I7** every KG effect references a real entity;
    * **I8** no nondeterministic primitives in any ``impl`` source.
    """
    violations: list[ConformanceViolation] = []
    violations += _check_i1(result)
    violations += _check_i2(result)
    violations += _check_i3(result)
    violations += _check_i4(result)
    violations += _check_i5(result)
    violations += _check_i6(result)
    violations += _check_i7(result)
    violations += _check_i8(result)
    return violations


def assert_conforms(result: RunResult) -> None:
    """Assert ``result`` passes the whole I-suite, raising on the first failure set."""
    violations = check_conformance(result)
    if violations:
        joined = "\n".join(f"  - {v}" for v in violations)
        raise AssertionError(f"conformance failed ({len(violations)} issue(s)):\n{joined}")


def _check_i1(result: RunResult) -> list[ConformanceViolation]:
    """I1 — timestamps in window + working hours."""
    out: list[ConformanceViolation] = []
    for event in result.journal:
        stamp = event.timestamp.isoformat()
        if not (result.start <= event.timestamp <= result.end):
            out.append(
                ConformanceViolation("I1", f"event at {stamp} is outside the window", event.id)
            )
        elif not result.calendar.is_working(event.timestamp):
            out.append(
                ConformanceViolation("I1", f"event at {stamp} is outside working hours", event.id)
            )
    return out


def _check_i2(result: RunResult) -> list[ConformanceViolation]:
    """I2 — monotonic timeline: a causal child never precedes its parent."""
    out: list[ConformanceViolation] = []
    by_id = {e.id: e for e in result.journal}
    for event in result.journal:
        if event.parent_event is None:
            continue
        parent = by_id.get(event.parent_event)
        if parent is not None and event.timestamp < parent.timestamp:
            out.append(
                ConformanceViolation(
                    "I2",
                    f"event precedes its parent {parent.id} "
                    f"({event.timestamp.isoformat()} < {parent.timestamp.isoformat()})",
                    event.id,
                )
            )
    return out


def _check_i3(result: RunResult) -> list[ConformanceViolation]:
    """I3 — participants ⊆ bound roles (real entities; exclude/distinct respected)."""
    out: list[ConformanceViolation] = []
    for event in result.journal:
        scope = result._type_scope.get(event.type)
        if scope is None:
            continue
        for role, people in event.actors.items():
            if role not in scope.role_selectors:
                out.append(
                    ConformanceViolation(
                        "I3", f"event uses undeclared role {role!r}", event.id
                    )
                )
                continue
            if len(set(people)) != len(people):
                selector = scope.role_selectors[role]
                if selector is None or selector.distinct:
                    out.append(
                        ConformanceViolation(
                            "I3", f"role {role!r} has duplicate participants", event.id
                        )
                    )
            selector = scope.role_selectors[role]
            for pid in people:
                if result.world.get_node(pid) is None:
                    out.append(
                        ConformanceViolation(
                            "I3", f"participant {pid!r} is not a real entity", event.id
                        )
                    )
                    continue
                if selector is not None and pid in selector.exclude:
                    out.append(
                        ConformanceViolation(
                            "I3", f"participant {pid!r} is in role {role!r} exclude list", event.id
                        )
                    )
                if selector is not None:
                    node = result.world.get_node(pid)
                    assert node is not None
                    for match in selector.where:
                        if not Filter(match.field, match.op, match.value).matches(node):
                            out.append(
                                ConformanceViolation(
                                    "I3",
                                    f"participant {pid!r} fails role {role!r} filter "
                                    f"{match.field} {match.op} {match.value!r}",
                                    event.id,
                                )
                            )
    return out


def _check_i4(result: RunResult) -> list[ConformanceViolation]:
    """I4 — comment threads well-formed (a reply resolves to an earlier event)."""
    out: list[ConformanceViolation] = []
    by_id = {e.id: e for e in result.journal}
    for event in result.journal:
        parent_id = event.payload.get("in_reply_to") or event.parent_event
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            out.append(
                ConformanceViolation(
                    "I4", f"reply parent {parent_id!r} does not resolve to an event", event.id
                )
            )
        elif parent.timestamp > event.timestamp:
            out.append(
                ConformanceViolation(
                    "I4", f"reply is earlier than its parent {parent_id}", event.id
                )
            )
    return out


def _check_i5(result: RunResult) -> list[ConformanceViolation]:
    """I5 — dynamic ``declares`` conformance: the run emits nothing it did not declare.

    Driven by the **real run** (the §13 "dynamic" check, critical for ``impl``): every
    event the journal actually emitted, and every deliverable it actually produced,
    must appear in the producing process's ``declares`` block. Effects do not surface
    in the journal, so they are checked structurally from the steps (the best available
    proxy). The engine *trusts* ``declares`` for graph soundness, so a run exceeding it
    is the dangerous direction this catches; a declared-but-unfired branch is allowed.
    """
    out: list[ConformanceViolation] = []
    for event in result.journal:
        scope = result._type_scope.get(event.type)
        if scope is None:
            continue
        declares = scope.process.declares
        if declares.events and event.type not in declares.events:
            out.append(
                ConformanceViolation(
                    "I5", f"{scope.process.name!r} emits undeclared event {event.type!r}", event.id
                )
            )
        if (
            event.deliverable is not None
            and declares.deliverables
            and event.deliverable.kind not in declares.deliverables
        ):
            out.append(
                ConformanceViolation(
                    "I5",
                    f"{scope.process.name!r} produces undeclared deliverable "
                    f"{event.deliverable.kind!r}",
                    event.id,
                )
            )
    # Effects are not journalled; check them structurally against declares.effects.
    for scope in result._scopes:
        if scope.process.declares.effects:
            declared_eff = set(scope.process.declares.effects)
            for sig in _effect_signatures(scope.process):
                if sig not in declared_eff:
                    out.append(
                        ConformanceViolation(
                            "I5", f"{scope.process.name!r} applies undeclared effect {sig!r}"
                        )
                    )
    return out


def _check_i6(result: RunResult) -> list[ConformanceViolation]:
    """I6 — determinism: a second seeded run from a fresh world emits the same stream."""
    if result.rerun().dumps() != result.journal.dumps():
        return [ConformanceViolation("I6", "re-running the same seed produced a different stream")]
    return []


def _check_i7(result: RunResult) -> list[ConformanceViolation]:
    """I7 — KG effects reference real entities (post-run world)."""
    out: list[ConformanceViolation] = []
    for scope in result._scopes:
        for step in scope.process.steps:
            for effect in step.effects:
                targets: list[str] = []
                if effect.kind is sdk.EffectKind.MUTATE:
                    targets = [effect.target]
                elif effect.kind is sdk.EffectKind.ADD_EDGE:
                    targets = [effect.src, effect.dst]
                elif effect.kind is sdk.EffectKind.CREATE_NODE:
                    targets = [effect.target]
                for node_id in targets:
                    if node_id and result.world.get_node(node_id) is None:
                        out.append(
                            ConformanceViolation(
                                "I7",
                                f"{scope.process.name!r} effect references missing entity",
                                node_id,
                            )
                        )
    return out


def _check_i8(result: RunResult) -> list[ConformanceViolation]:
    """I8 — no nondeterministic primitives in any ``impl`` source (best-effort scan)."""
    out: list[ConformanceViolation] = []
    for scope in result._scopes:
        impl = scope.process.impl
        if impl is None:
            continue
        source = _impl_source(impl)
        if source is None:
            continue
        for finding in scan_nondeterminism(source):
            out.append(
                ConformanceViolation("I8", f"{scope.process.name!r} impl uses {finding}", impl)
            )
    return out


def _effect_signatures(process: sdk.Process) -> set[str]:
    """The ``declares.effects`` signatures a process's steps imply (``mutate:attr`` …)."""
    sigs: set[str] = set()
    for step in process.steps:
        for effect in step.effects:
            if effect.kind is sdk.EffectKind.MUTATE:
                sigs.add(f"mutate:{effect.attr}")
            elif effect.kind is sdk.EffectKind.MILESTONE:
                sigs.add(f"milestone:{effect.name}")
            elif effect.kind is sdk.EffectKind.CREATE_NODE:
                sigs.add(f"create:{effect.node_type}")
            elif effect.kind is sdk.EffectKind.ADD_EDGE:
                sigs.add(f"add_edge:{effect.edge_type}")
    return sigs


# --------------------------------------------------------------------------- #
# Nondeterminism scanner (I8) — an AST check for wall-clock / unseeded random.
# --------------------------------------------------------------------------- #

#: Dotted call targets that make an ``impl`` nondeterministic (D26 forbids these).
_FORBIDDEN_CALLS = {
    "time.time",
    "time.monotonic",
    "time.time_ns",
    "datetime.now",
    "datetime.utcnow",
    "datetime.today",
    "random.random",
    "random.randint",
    "random.choice",
    "random.shuffle",
    "random.uniform",
    "uuid.uuid1",
    "uuid.uuid4",
    "os.urandom",
}


def scan_nondeterminism(source: str) -> list[str]:
    """Return the forbidden nondeterministic primitives ``source`` calls (I8).

    A lightweight AST scan (§13 "AST rule forbids wall-clock / unseeded random"):
    flags calls to wall-clock and unseeded-random APIs by their trailing dotted name
    (``datetime.now``, ``random.random``, ``uuid.uuid4`` …). A ``random.Random``
    instance seeded by the caller is the deterministic path and is *not* flagged —
    only the module-level conveniences are. Returns a sorted, de-duplicated list of
    the dotted names found (empty ⇒ clean).
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted is None:
            continue
        # Match on the trailing two components (module.attr) so aliasing the import
        # path (``from datetime import datetime``) still trips the rule.
        tail = ".".join(dotted.split(".")[-2:])
        if tail in _FORBIDDEN_CALLS:
            found.add(tail)
    return sorted(found)


def _dotted_name(node: ast.expr) -> str | None:
    """Reconstruct a dotted attribute/name chain (``a.b.c``) from an AST node."""
    parts: list[str] = []
    current: ast.expr | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _impl_source(impl: str) -> str | None:
    """Load the source of an ``impl`` ``pkg.module:ClassName`` ref, or ``None`` if absent."""
    module_path, _, _ = impl.partition(":")
    try:
        module = __import__(module_path, fromlist=["_"])
        return inspect.getsource(module)
    except (ImportError, OSError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Playbook conformance suite — P1–P6 (§13).
# --------------------------------------------------------------------------- #


def check_playbook(playbook: sdk.Playbook) -> list[ConformanceViolation]:
    """Run the static playbook suite (P1–P6) over ``playbook`` (§13).

    These judge the *triggering graph* (mostly static — they cover ``impl`` processes
    through ``declares`` too):

    * **P1** every ``OnEvent`` trigger has an emitter (no dead trigger);
    * **P2** every activation is reachable from a seeding trigger;
    * **P3** trigger cycles are guarded (a milestone/condition gate breaks the loop);
    * **P4** ``deliverable_expectations`` are all covered by some process;
    * **P5** staffing is feasible (each selector's minimum is satisfiable);
    * **P6** cadence/probabilistic volume stays within bounds.
    """
    out: list[ConformanceViolation] = []
    out += _check_p1(playbook)
    out += _check_p2(playbook)
    out += _check_p3(playbook)
    out += _check_p4(playbook)
    out += _check_p5(playbook)
    out += _check_p6(playbook)
    return out


def _emitted_types(playbook: sdk.Playbook) -> set[str]:
    """Every event type any process in the playbook emits (steps + spreads + declares)."""
    types: set[str] = set()
    for act in playbook.activations:
        proc = act.process
        types.update(proc.declares.events)
        for step in proc.steps:
            for emitted in step.emits:
                types.add(emitted.type)
            if step.repeat is not None:
                types.add(step.repeat.emits)
    return types


def _emitted_milestones(playbook: sdk.Playbook) -> set[str]:
    """Every milestone name any process announces (steps + declares)."""
    names: set[str] = set()
    for act in playbook.activations:
        for effect_sig in act.process.declares.effects:
            if effect_sig.startswith("milestone:"):
                names.add(effect_sig.split(":", 1)[1])
        for step in act.process.steps:
            for effect in step.effects:
                if effect.kind is sdk.EffectKind.MILESTONE:
                    names.add(effect.name)
    return names


def _check_p1(playbook: sdk.Playbook) -> list[ConformanceViolation]:
    """P1 — every ``OnEvent`` trigger has some emitter (no dead trigger)."""
    emitted = _emitted_types(playbook)
    out: list[ConformanceViolation] = []
    for act in playbook.activations:
        if isinstance(act.trigger, sdk.OnEvent) and act.trigger.type not in emitted:
            out.append(
                ConformanceViolation(
                    "P1", f"OnEvent({act.trigger.type!r}) has no emitter (dead trigger)", act.id
                )
            )
    return out


def _check_p2(playbook: sdk.Playbook) -> list[ConformanceViolation]:
    """P2 — every activation is reachable from a seeding trigger (fixpoint over the graph)."""
    reachable: set[str] = set()
    available_events: set[str] = set()
    available_milestones: set[str] = set()

    def emits_of(act: sdk.Activation) -> tuple[set[str], set[str]]:
        evs: set[str] = set(act.process.declares.events)
        mls: set[str] = set()
        for step in act.process.steps:
            for emitted in step.emits:
                evs.add(emitted.type)
            if step.repeat is not None:
                evs.add(step.repeat.emits)
            for effect in step.effects:
                if effect.kind is sdk.EffectKind.MILESTONE:
                    mls.add(effect.name)
        for sig in act.process.declares.effects:
            if sig.startswith("milestone:"):
                mls.add(sig.split(":", 1)[1])
        return evs, mls

    # Iterate to a fixpoint: seed roots, then anything a reachable activation enables.
    changed = True
    while changed:
        changed = False
        for act in playbook.activations:
            if act.id in reachable:
                continue
            trig = act.trigger
            root_triggers = (sdk.OnStart, sdk.OnCadence, sdk.OnCondition, sdk.Probabilistic)
            is_root = isinstance(trig, root_triggers)
            fires = (
                is_root
                or (isinstance(trig, sdk.OnEvent) and trig.type in available_events)
                or (isinstance(trig, sdk.OnMilestone) and trig.name in available_milestones)
            )
            if fires:
                reachable.add(act.id)
                evs, mls = emits_of(act)
                available_events |= evs
                available_milestones |= mls
                changed = True

    return [
        ConformanceViolation("P2", "activation is unreachable from any seeding trigger", act.id)
        for act in playbook.activations
        if act.id not in reachable
    ]


def _check_p3(playbook: sdk.Playbook) -> list[ConformanceViolation]:
    """P3 — trigger cycles are guarded by a milestone/condition gate (else runaway risk)."""
    # Build the OnEvent dependency graph: act -> acts it can trigger via emitted events.
    adjacency: dict[str, set[str]] = {a.id: set() for a in playbook.activations}
    by_event: dict[str, list[str]] = {}
    for act in playbook.activations:
        if isinstance(act.trigger, sdk.OnEvent):
            by_event.setdefault(act.trigger.type, []).append(act.id)
    for act in playbook.activations:
        evs: set[str] = set(act.process.declares.events)
        for step in act.process.steps:
            for emitted in step.emits:
                evs.add(emitted.type)
            if step.repeat is not None:
                evs.add(step.repeat.emits)
        for ev in evs:
            for target in by_event.get(ev, ()):
                adjacency[act.id].add(target)

    cycle = _find_cycle(adjacency)
    if cycle is None:
        return []
    # A cycle is acceptable only if some activation in it is condition/milestone gated.
    gated = any(
        isinstance(_activation_by_id(playbook, aid).trigger, (sdk.OnCondition, sdk.OnMilestone))
        for aid in cycle
    )
    if gated:
        return []
    return [
        ConformanceViolation(
            "P3", f"unguarded trigger cycle: {' -> '.join(cycle)} (runaway risk)", cycle[0]
        )
    ]


def _check_p4(playbook: sdk.Playbook) -> list[ConformanceViolation]:
    """P4 — every declared deliverable expectation is produced by some process."""
    produced: set[str] = set()
    for act in playbook.activations:
        produced.update(act.process.declares.deliverables)
        for step in act.process.steps:
            if step.produces is not None:
                produced.add(step.produces.kind)
    return [
        ConformanceViolation("P4", f"deliverable expectation {kind!r} is never produced")
        for kind in playbook.deliverable_expectations
        if kind not in produced
    ]


def _check_p5(playbook: sdk.Playbook) -> list[ConformanceViolation]:
    """P5 — staffing feasible: a synthesised world yields ≥ the minimum for each selector."""
    out: list[ConformanceViolation] = []
    world = TestWorld.for_playbook(playbook).world
    seen: set[tuple[str, str]] = set()
    for act in playbook.activations:
        for role in act.process.roles:
            if role.select is None:
                continue
            key = (act.process.name, role.name)
            if key in seen:
                continue
            seen.add(key)
            lo, _ = _count_range(role.select.count)
            candidates = [
                node
                for node in world.nodes_by_type(role.select.type)
                if all(
                    Filter(m.field, m.op, m.value).matches(node) for m in role.select.where
                )
            ]
            if len(candidates) < lo:
                out.append(
                    ConformanceViolation(
                        "P5",
                        f"role {role.name!r} in {act.process.name!r} needs {lo} but only "
                        f"{len(candidates)} candidate(s) are feasible",
                    )
                )
    return out


def _check_p6(playbook: sdk.Playbook, *, max_events: int = 10_000) -> list[ConformanceViolation]:
    """P6 — cadence/probabilistic volume stays within bounds over a 12-week window."""
    weeks = DEFAULT_WINDOW.days / 7.0
    out: list[ConformanceViolation] = []
    total = 0.0
    for act in playbook.activations:
        steps = max(len(act.process.steps), 1)
        trig = act.trigger
        firings = 0.0
        if isinstance(trig, sdk.OnCadence):
            firings = _cadence_firings_estimate(trig.rule, weeks)
        elif isinstance(trig, sdk.Probabilistic):
            per_week = {"day": 5.0, "week": 1.0, "sprint": 0.5, "month": 0.25}.get(trig.per, 1.0)
            firings = trig.rate * per_week * weeks
        total += firings * steps
    if total > max_events:
        out.append(
            ConformanceViolation(
                "P6", f"estimated ~{int(total)} events exceeds the {max_events} volume bound"
            )
        )
    return out


def _cadence_firings_estimate(rule: str, weeks: float) -> float:
    """A rough firing count for a cadence ``rule`` over ``weeks`` (volume heuristic)."""
    kind, _, arg = rule.partition(":")
    kind = kind.strip().lower()
    if kind == "daily":
        return weeks * 5.0
    if kind == "weekly":
        return weeks
    if kind in ("every", "per_sprint"):
        token = arg.strip().upper()
        unit = token[-1] if token else "W"
        try:
            n = int(token[:-1]) if token[:-1] else 2
        except ValueError:
            n = 2
        period_weeks = n if unit == "W" else n / 7.0
        return weeks / period_weeks if period_weeks else weeks
    return weeks


def _find_cycle(adjacency: Mapping[str, set[str]]) -> list[str] | None:
    """Return one cycle in a directed graph as an id path, or ``None`` if acyclic."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in adjacency}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(adjacency.get(node, ())):
            if color.get(nxt, WHITE) == GRAY:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(adjacency):
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def _activation_by_id(playbook: sdk.Playbook, act_id: str) -> sdk.Activation:
    """Look up an activation by id (helper for the P3 gate check)."""
    for act in playbook.activations:
        if act.id == act_id:
            return act
    raise KeyError(act_id)


# --------------------------------------------------------------------------- #
# Golden snapshots (§13 "golden snapshots of the seeded, stable stream").
# --------------------------------------------------------------------------- #


def snapshot(result: RunResult) -> str:
    """The canonical JSONL snapshot of a run's event stream (deterministic)."""
    return result.snapshot()


def assert_golden(result: RunResult, path: str | Path) -> None:
    """Assert ``result``'s stream matches the golden file at ``path``.

    The golden file is the seeded, stable event stream a process is expected to
    produce — a regression tripwire. It is (re)written when missing or when the
    ``ESIM_UPDATE_GOLDEN`` env var is set (record mode); otherwise the current run is
    compared against it and any drift raises with a unified-diff-style message.
    """
    golden = Path(path)
    current = result.snapshot()
    if os.environ.get(_GOLDEN_UPDATE_ENV) or not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(current, encoding="utf-8")
        return
    expected = golden.read_text(encoding="utf-8")
    if current != expected:
        raise AssertionError(
            f"golden mismatch for {golden}\n"
            f"expected {expected.count(chr(10))} event(s), got {current.count(chr(10))}; "
            f"re-run with {_GOLDEN_UPDATE_ENV}=1 to update if this change is intended"
        )
