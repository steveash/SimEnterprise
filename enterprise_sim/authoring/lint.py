"""Tier 1 — static lint / type-check / event-graph checks (ARCHITECTURE §13, D23).

``enterprise-sim lint`` runs **without executing** a playbook: it reads the
declarative authoring objects (:mod:`enterprise_sim.authoring.sdk`) and reports
the static defects that make a process or playbook *wrong by construction*,
before the test kit (Tier 2) or evaluators (Tier 3) ever run. A process/playbook
is "not done until Tier 1 is clean".

The checks mirror §13 Tier 1 one-for-one, grouped into six families:

* **Type / schema** — timing strings (``at`` / ``duration`` / ``offset`` /
  ``slot``), ``count`` / ``per_actor`` ranges, ``OnCadence`` rules, ``OnCondition``
  / ``Match`` operators, and ``Probabilistic`` rates all parse and are sane. The
  authoring layer is already typed dataclasses, so this layer re-checks the
  *string-encoded* expressions the type system cannot (and reuses the engine's
  own parsers in :mod:`enterprise_sim.core.sim.spec` so "parses to the linter"
  means exactly "parses to the scheduler").
* **Reference integrity** — every ``by`` / ``repeat.role`` resolves to a role the
  process binds; every ``after`` / ``parent_step`` targets an existing step; step
  dependencies are acyclic; step and activation ids are unique; ``rank_by`` names a
  real ranking signal.
* **``declares`` conformance (static)** — for a *declarative* process the events it
  emits, the deliverable kinds it produces, and the effects it applies must match
  its ``declares`` block (the engine trusts ``declares``): emitting something
  undeclared is an **error** (the engine would never see it); declaring something
  never emitted is a **warning**. ``impl``-backed processes have no steps to read,
  so their ``declares`` is trusted here and checked dynamically by conformance I5.
* **Event-graph soundness** — **dead triggers** (an ``OnEvent`` whose type no
  process emits, P1), **unreachable processes** (no path from a start / cadence /
  condition / probabilistic / external-milestone root, P2), and **unguarded
  cycles** (an event cycle with no damping guard — runaway risk, P3).
* **Feasibility & volume** — count ranges are satisfiable and ``Probabilistic``
  rates won't explode artifact counts (the cost linter).
* **Determinism** — an AST rule (D23) forbidding wall-clock reads and unseeded
  randomness in ``impl`` code; :func:`scan_impl_source` scans a module's source
  and :func:`lint_playbook` applies it to every resolvable ``impl`` ref.

The public surface is small: :class:`Diagnostic` (one finding), :class:`LintResult`
(the collected findings with an ``ok`` verdict), and the entry points
:func:`lint_playbook`, :func:`lint_process`, and :func:`scan_impl_source`.
"""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from enterprise_sim.authoring.sdk import (
    Activation,
    ConditionExpr,
    EffectKind,
    KGEffect,
    Match,
    OnCadence,
    OnCondition,
    OnEvent,
    OnMilestone,
    OnStart,
    Playbook,
    Probabilistic,
    Process,
    Step,
)
from enterprise_sim.core.sim.spec import (
    parse_business_days,
    parse_duration_hours,
    parse_int_range,
)

__all__ = [
    "Diagnostic",
    "LintResult",
    "Severity",
    "lint_playbook",
    "lint_process",
    "scan_impl_source",
]

#: Operators a :class:`Match` / :class:`ConditionExpr` may use (mirrors the
#: engine ``FilterOp``; kept literal so the linter needs no engine import).
_VALID_OPS = frozenset({"eq", "ne", "in", "contains", "gte", "lte"})

#: Ranking signals a selector's ``rank_by`` may name (mirrors engine ``RankSignal``).
_VALID_RANK_SIGNALS = frozenset({"affinity", "inverse_load", "expertise"})

#: Periods a :class:`Probabilistic` trigger may use.
_VALID_PER = frozenset({"day", "week", "sprint", "month"})

#: Weekdays a ``weekly:DOW`` cadence rule may name.
_WEEKDAYS = frozenset({"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"})

#: Expected mean arrivals *per window* above which a ``Probabilistic`` stream is
#: flagged as a likely artifact explosion (the cost linter's volume ceiling).
_VOLUME_CEILING = 200.0

#: Rough per-window arrival multipliers for the cost heuristic (a 90-day default
#: window): how many of each ``per`` period fit, used to project total firings.
_PERIODS_PER_WINDOW = {"day": 90.0, "week": 13.0, "sprint": 6.0, "month": 3.0}

#: Module-qualified attribute reads that are wall-clock / unseeded-random sources
#: forbidden in ``impl`` code (D23). Keyed by ``(module, attr)``.
_FORBIDDEN_CALLS = frozenset(
    {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
        ("date", "today"),
        ("time", "time"),
        ("time", "monotonic"),
        ("time", "perf_counter"),
        ("time", "time_ns"),
        ("random", "random"),
        ("random", "randint"),
        ("random", "randrange"),
        ("random", "choice"),
        ("random", "choices"),
        ("random", "shuffle"),
        ("random", "sample"),
        ("random", "uniform"),
        ("random", "gauss"),
        ("uuid", "uuid1"),
        ("uuid", "uuid4"),
        ("os", "urandom"),
    }
)

#: Whole modules whose every member is a forbidden nondeterminism source.
_FORBIDDEN_MODULES = frozenset({"secrets"})


class Severity(StrEnum):
    """How serious a :class:`Diagnostic` is.

    ``ERROR`` means the playbook is wrong by construction — the engine would
    misbehave or refuse it, so :attr:`LintResult.ok` is ``False``. ``WARNING``
    means a likely defect (over-declaration, runaway risk, suspicious volume) that
    does not by itself block a run.
    """

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One lint finding: a coded, located, human-readable message.

    Attributes:
        code: Stable machine code (``"dead-trigger"``, ``"under-declared-event"``
            …) so callers and tests can assert on a defect class without parsing
            prose.
        severity: :class:`Severity` — errors fail the lint, warnings do not.
        message: Human-readable description of the problem.
        location: Dotted path to the offending object
            (``"build_software/design_review/review"``), or ``""`` at top level.
    """

    code: str
    severity: Severity
    message: str
    location: str = ""

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity.value}: {self.code}: {self.message}{where}"


@dataclass(frozen=True, slots=True)
class LintResult:
    """The collected diagnostics for a lint run, with an ``ok`` verdict.

    ``ok`` is ``True`` iff there are no :attr:`Severity.ERROR` diagnostics, so a
    warning-only run still passes. :meth:`errors` / :meth:`warnings` partition the
    findings by severity.
    """

    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        """``True`` iff no diagnostic is an error."""
        return not any(d.severity is Severity.ERROR for d in self.diagnostics)

    def errors(self) -> tuple[Diagnostic, ...]:
        """The error-severity diagnostics, in report order."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.ERROR)

    def warnings(self) -> tuple[Diagnostic, ...]:
        """The warning-severity diagnostics, in report order."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.WARNING)

    def codes(self) -> frozenset[str]:
        """The set of distinct diagnostic codes present (handy in tests)."""
        return frozenset(d.code for d in self.diagnostics)


# --------------------------------------------------------------------------- #
# Internal collector — accumulates diagnostics as checks run.
# --------------------------------------------------------------------------- #


class _Collector:
    """Mutable sink the check functions append diagnostics to."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def error(self, code: str, message: str, location: str = "") -> None:
        self._items.append(Diagnostic(code, Severity.ERROR, message, location))

    def warning(self, code: str, message: str, location: str = "") -> None:
        self._items.append(Diagnostic(code, Severity.WARNING, message, location))

    def extend(self, diagnostics: list[Diagnostic]) -> None:
        self._items.extend(diagnostics)

    def result(self) -> LintResult:
        return LintResult(tuple(self._items))


# --------------------------------------------------------------------------- #
# Type / schema checks — the string-encoded expressions the types can't catch.
# --------------------------------------------------------------------------- #


def _check_cadence_rule(rule: str, c: _Collector, loc: str) -> None:
    """Validate an ``OnCadence`` rule against the scheduler's accepted grammar."""
    kind, sep, arg = rule.partition(":")
    kind = kind.strip().lower()
    arg = arg.strip().upper()
    if kind == "daily":
        if arg and arg != "WORKDAYS":
            c.error("bad-cadence", f"unknown daily cadence rule: {rule!r}", loc)
        return
    if kind == "weekly":
        if arg not in _WEEKDAYS:
            c.error("bad-cadence", f"unknown weekday in cadence rule: {rule!r}", loc)
        return
    if kind in ("every", "per_sprint"):
        token = arg if kind == "every" else (arg if arg.endswith(("W", "D")) else arg + "W")
        if kind == "per_sprint" and not arg:
            token = "2W"
        if not token or token[-1] not in ("W", "D"):
            c.error("bad-cadence", f"cadence interval needs a w/d unit: {rule!r}", loc)
            return
        try:
            n = int(token[:-1])
        except ValueError:
            c.error("bad-cadence", f"malformed cadence interval: {rule!r}", loc)
            return
        if n <= 0:
            c.error("bad-cadence", f"cadence interval must be positive: {rule!r}", loc)
        return
    c.error("bad-cadence", f"unknown cadence rule: {rule!r}", loc)


def _check_timing(step: Step, c: _Collector, loc: str) -> None:
    """Validate a step's timing strings parse against the working calendar."""
    if step.at is not None and step.after is not None:
        c.error(
            "conflicting-timing",
            f"step {step.id!r} sets both 'at' and 'after' (mutually exclusive)",
            loc,
        )
    if step.at is not None:
        try:
            parse_business_days(step.at)
        except ValueError as exc:
            c.error("bad-timing", f"invalid 'at': {exc}", loc)
    # ``duration`` / ``offset`` / ``slot`` are working-time spans; hours_per_day is
    # irrelevant to *whether* they parse, so any positive value works here.
    for label, value in (("offset", step.offset), ("duration", step.duration), ("slot", step.slot)):
        if value is None:
            continue
        try:
            parse_duration_hours(value, hours_per_day=8.0)
        except ValueError as exc:
            c.error("bad-timing", f"invalid {label!r}: {exc}", loc)


def _check_count(spec: int | str, c: _Collector, loc: str, what: str) -> None:
    """Validate a ``count`` / ``per_actor`` spec parses to a sane ``(lo, hi)``."""
    try:
        parse_int_range(spec)
    except ValueError as exc:
        c.error("bad-count", f"invalid {what}: {exc}", loc)


def _check_match(m: Match, c: _Collector, loc: str) -> None:
    if m.op not in _VALID_OPS:
        c.error("bad-operator", f"unknown match operator {m.op!r} on field {m.field!r}", loc)
    if not m.field:
        c.error("bad-operator", "match predicate has an empty field name", loc)


def _check_condition(expr: ConditionExpr, c: _Collector, loc: str) -> None:
    if expr.op not in _VALID_OPS:
        c.error("bad-condition", f"unknown condition operator {expr.op!r}", loc)
    if not expr.node or not expr.attr:
        c.error("bad-condition", "condition needs a non-empty node and attr", loc)


# --------------------------------------------------------------------------- #
# Effect signatures — how a KGEffect lowers to a ``declares.effects`` string.
# --------------------------------------------------------------------------- #


def _effect_signature(effect: KGEffect) -> str:
    """The ``declares.effects`` token a :class:`KGEffect` corresponds to.

    Mirrors the patterns' convention: a ``MUTATE`` of ``attr`` declares
    ``"mutate:attr"``; a ``MILESTONE`` named ``n`` declares ``"milestone:n"``; a
    ``CREATE_NODE`` / ``ADD_EDGE`` declares ``"create_node:type"`` /
    ``"add_edge:type"``.
    """
    if effect.kind is EffectKind.MUTATE:
        return f"mutate:{effect.attr}"
    if effect.kind is EffectKind.MILESTONE:
        return f"milestone:{effect.name}"
    if effect.kind is EffectKind.CREATE_NODE:
        return f"create_node:{effect.node_type}"
    if effect.kind is EffectKind.ADD_EDGE:
        return f"add_edge:{effect.edge_type}"
    return str(effect.kind)


# --------------------------------------------------------------------------- #
# Per-process structural facts the playbook-level checks also reuse.
# --------------------------------------------------------------------------- #


def _emitted_events(process: Process) -> frozenset[str]:
    """Event types a process declares (its ``declares.events``).

    The engine trusts ``declares`` for graph wiring, so an activation's emissions
    are read from there — for both declarative and ``impl`` processes.
    """
    return frozenset(process.declares.events)


def _emitted_milestones(process: Process) -> frozenset[str]:
    """Milestone names a process declares (``milestone:`` entries in declares)."""
    return frozenset(
        e.split(":", 1)[1] for e in process.declares.effects if e.startswith("milestone:")
    )


def _step_emitted_events(process: Process) -> frozenset[str]:
    """Event types the *steps* actually emit (declarative processes only)."""
    out: set[str] = set()
    for step in process.steps:
        out.update(e.type for e in step.emits)
        if step.repeat is not None:
            out.add(step.repeat.emits)
    return frozenset(out)


# --------------------------------------------------------------------------- #
# Reference integrity + declares conformance for a single process.
# --------------------------------------------------------------------------- #


def _check_process_refs(process: Process, c: _Collector, base: str) -> None:
    """Reference integrity: roles resolve, step targets exist, deps are acyclic."""
    role_names = {r.name for r in process.roles}

    # Role uniqueness.
    seen_roles: set[str] = set()
    for role in process.roles:
        if role.name in seen_roles:
            c.error("duplicate-role", f"duplicate role {role.name!r}", base)
        seen_roles.add(role.name)
        if role.select is not None:
            sel = role.select
            loc = f"{base}/role:{role.name}"
            for sig in sel.rank_by:
                if sig not in _VALID_RANK_SIGNALS:
                    c.error("bad-rank-signal", f"unknown rank_by signal {sig!r}", loc)
            for m in sel.where:
                _check_match(m, c, loc)
            _check_count(sel.count, c, loc, "selector count")

    # ``impl`` and ``steps`` are mutually exclusive; one must be present.
    if process.impl is not None and process.steps:
        c.error(
            "impl-and-steps",
            f"process {process.name!r} sets both 'impl' and 'steps'",
            base,
        )
    if process.impl is None and not process.steps:
        c.warning("empty-process", f"process {process.name!r} has no impl and no steps", base)

    step_ids = {s.id for s in process.steps}
    seen_steps: set[str] = set()
    for step in process.steps:
        loc = f"{base}/{step.id}"
        if step.id in seen_steps:
            c.error("duplicate-step", f"duplicate step id {step.id!r}", base)
        seen_steps.add(step.id)

        _check_timing(step, c, loc)

        if step.by is not None and step.by not in role_names:
            c.error("unknown-role", f"step {step.id!r} acts 'by' unknown role {step.by!r}", loc)
        if step.after is not None and step.after not in step_ids:
            c.error("bad-after", f"step {step.id!r} 'after' unknown step {step.after!r}", loc)
        if step.parent_step is not None and step.parent_step not in step_ids:
            c.error(
                "bad-parent",
                f"step {step.id!r} 'parent_step' unknown step {step.parent_step!r}",
                loc,
            )
        if step.repeat is not None:
            if step.repeat.role not in role_names:
                c.error(
                    "unknown-role",
                    f"step {step.id!r} repeat uses unknown role {step.repeat.role!r}",
                    loc,
                )
            _check_count(step.repeat.per_actor, c, loc, "repeat per_actor")
        if step.when is not None:
            _check_condition(step.when, c, loc)

    _check_step_cycles(process, c, base)


def _check_step_cycles(process: Process, c: _Collector, base: str) -> None:
    """Flag a cycle in the ``after`` / ``parent_step`` step-dependency graph."""
    step_ids = {s.id for s in process.steps}
    deps: dict[str, set[str]] = {s.id: set() for s in process.steps}
    for step in process.steps:
        for dep in (step.after, step.parent_step):
            if dep is not None and dep in step_ids and dep != step.id:
                deps[step.id].add(dep)
            elif dep == step.id:
                c.error("step-cycle", f"step {step.id!r} depends on itself", base)

    # Iterative DFS cycle detection (white/grey/black colouring).
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(deps, WHITE)

    def visit(start: str) -> bool:
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                color[node] = BLACK
                continue
            if color[node] == GREY:
                continue
            color[node] = GREY
            stack.append((node, True))
            for nxt in deps[node]:
                if color[nxt] == GREY:
                    return True
                if color[nxt] == WHITE:
                    stack.append((nxt, False))
        return False

    for sid in deps:
        if color[sid] == WHITE and visit(sid):
            c.error("step-cycle", f"cyclic step dependencies in process {process.name!r}", base)
            return


def _check_declares_conformance(process: Process, c: _Collector, base: str) -> None:
    """Static ``declares`` conformance for a declarative process (§13).

    ``impl`` processes have no steps to read, so their ``declares`` is trusted
    here (conformance I5 checks it dynamically).
    """
    if not process.steps:
        return

    declared_events = set(process.declares.events)
    declared_deliverables = set(process.declares.deliverables)
    declared_effects = set(process.declares.effects)

    actual_events = _step_emitted_events(process)
    actual_deliverables = {s.produces.kind for s in process.steps if s.produces is not None}
    actual_effects = {_effect_signature(e) for s in process.steps for e in s.effects}

    # Under-declaration is an error: the engine trusts ``declares`` and would never
    # see the undeclared emission.
    for ev in sorted(actual_events - declared_events):
        c.error("under-declared-event", f"emits {ev!r} but it is not in declares.events", base)
    for dk in sorted(actual_deliverables - declared_deliverables):
        c.error(
            "under-declared-deliverable",
            f"produces {dk!r} but it is not in declares.deliverables",
            base,
        )
    for ef in sorted(actual_effects - declared_effects):
        c.error("under-declared-effect", f"applies {ef!r} but it is not in declares.effects", base)

    # Over-declaration is a warning: declared but never produced by any step.
    for ev in sorted(declared_events - actual_events):
        c.warning("over-declared-event", f"declares event {ev!r} that no step emits", base)
    for dk in sorted(declared_deliverables - actual_deliverables):
        c.warning(
            "over-declared-deliverable",
            f"declares deliverable {dk!r} that no step produces",
            base,
        )
    for ef in sorted(declared_effects - actual_effects):
        c.warning("over-declared-effect", f"declares effect {ef!r} that no step applies", base)


# --------------------------------------------------------------------------- #
# Trigger schema checks.
# --------------------------------------------------------------------------- #


def _check_trigger_schema(activation: Activation, c: _Collector, loc: str) -> None:
    """Validate the activation's trigger's own fields (rate, rule, condition…)."""
    trig = activation.trigger
    if isinstance(trig, OnCadence):
        _check_cadence_rule(trig.rule, c, loc)
    elif isinstance(trig, OnCondition):
        _check_condition(trig.expr, c, loc)
    elif isinstance(trig, OnEvent):
        for m in trig.where:
            _check_match(m, c, loc)
        if not trig.type:
            c.error("bad-operator", "OnEvent has an empty event type", loc)
    elif isinstance(trig, Probabilistic):
        if not (trig.rate > 0):
            c.error("bad-rate", f"Probabilistic rate must be positive, got {trig.rate!r}", loc)
        if trig.per not in _VALID_PER:
            c.error("bad-rate", f"unknown Probabilistic period {trig.per!r}", loc)
    elif isinstance(trig, OnMilestone):
        if not trig.name:
            c.error("bad-operator", "OnMilestone has an empty name", loc)


def _check_volume(activation: Activation, c: _Collector, loc: str) -> None:
    """Cost linter: flag a ``Probabilistic`` stream likely to explode artifacts."""
    trig = activation.trigger
    if not isinstance(trig, Probabilistic):
        return
    if not (trig.rate > 0) or trig.per not in _PERIODS_PER_WINDOW:
        return
    projected = trig.rate * _PERIODS_PER_WINDOW[trig.per]
    if projected > _VOLUME_CEILING:
        c.warning(
            "volume-explosion",
            f"Probabilistic {trig.rate}/{trig.per} ~= {projected:.0f} firings/window "
            f"exceeds the {_VOLUME_CEILING:.0f} ceiling (artifact explosion risk)",
            loc,
        )


# --------------------------------------------------------------------------- #
# Event-graph soundness across a playbook's activations.
# --------------------------------------------------------------------------- #


def _check_event_graph(playbook: Playbook, c: _Collector) -> None:
    """Dead triggers, unreachable processes, and unguarded cycles (P1–P3)."""
    base = playbook.name

    # Every event any process emits, and every milestone any process announces.
    all_events: set[str] = set()
    all_milestones: set[str] = set()
    for act in playbook.activations:
        all_events |= _emitted_events(act.process)
        all_events |= _step_emitted_events(act.process)
        all_milestones |= _emitted_milestones(act.process)

    # --- Dead OnEvent triggers (P1) -------------------------------------- #
    dead_event_acts: set[str] = set()
    for act in playbook.activations:
        if isinstance(act.trigger, OnEvent) and act.trigger.type not in all_events:
            dead_event_acts.add(act.id)
            c.error(
                "dead-trigger",
                f"OnEvent {act.trigger.type!r} is emitted by no process",
                f"{base}/{act.id}",
            )

    # --- Reachability (P2) ----------------------------------------------- #
    # Roots fire autonomously: start / cadence / condition / probabilistic, plus an
    # OnMilestone whose milestone no process emits (a project-lifecycle milestone
    # supplied from outside the playbook).
    reachable: set[str] = set()
    pending = list(playbook.activations)

    def is_root(act: Activation) -> bool:
        t = act.trigger
        if isinstance(t, (OnStart, OnCadence, OnCondition, Probabilistic)):
            return True
        if isinstance(t, OnMilestone) and t.name not in all_milestones:
            return True
        return False

    for act in playbook.activations:
        if is_root(act):
            reachable.add(act.id)

    # Fixpoint: an activation becomes reachable once a reachable activation emits
    # the event / milestone it waits on.
    changed = True
    while changed:
        changed = False
        live_events: set[str] = set()
        live_milestones: set[str] = set()
        for act in playbook.activations:
            if act.id in reachable:
                live_events |= _emitted_events(act.process)
                live_events |= _step_emitted_events(act.process)
                live_milestones |= _emitted_milestones(act.process)
        for act in pending:
            if act.id in reachable:
                continue
            t = act.trigger
            if isinstance(t, OnEvent) and t.type in live_events:
                reachable.add(act.id)
                changed = True
            elif isinstance(t, OnMilestone) and t.name in live_milestones:
                reachable.add(act.id)
                changed = True

    for act in playbook.activations:
        # A dead OnEvent is already reported; don't double-flag it as unreachable.
        if act.id not in reachable and act.id not in dead_event_acts:
            c.warning(
                "unreachable-process",
                f"activation {act.id!r} ({act.process.name}) is triggered by nothing reachable",
                f"{base}/{act.id}",
            )

    _check_unguarded_cycles(playbook, c)


def _check_unguarded_cycles(playbook: Playbook, c: _Collector) -> None:
    """Flag event cycles with no damping guard (runaway risk, P3).

    The activation graph has an edge ``A -> B`` when ``A`` emits an event/milestone
    that triggers ``B``'s ``OnEvent`` / ``OnMilestone``. A cycle is *guarded* when
    some activation on it dampens re-firing — an ``OnEvent`` with a ``where``
    predicate, an ``OnCondition`` (only fires while a state holds), or a step guard
    (``when``). An unguarded cycle can fire forever.
    """
    acts = {a.id: a for a in playbook.activations}

    # Build the trigger edges.
    edges: dict[str, set[str]] = {a.id: set() for a in playbook.activations}
    for src in playbook.activations:
        events = _emitted_events(src.process) | _step_emitted_events(src.process)
        milestones = _emitted_milestones(src.process)
        for dst in playbook.activations:
            t = dst.trigger
            if isinstance(t, OnEvent) and t.type in events:
                edges[src.id].add(dst.id)
            elif isinstance(t, OnMilestone) and t.name in milestones:
                edges[src.id].add(dst.id)

    def is_guarded(act_id: str) -> bool:
        act = acts[act_id]
        t = act.trigger
        if isinstance(t, OnEvent) and t.where:
            return True
        if isinstance(t, OnCondition):
            return True
        return any(s.when is not None for s in act.process.steps)

    # Find strongly-connected components (Tarjan) to locate cycles, then report any
    # SCC (a true cycle) with no guard among its members. Self-loops count.
    cycles = _strongly_connected_cycles(edges)
    for scc in cycles:
        if not any(is_guarded(node) for node in scc):
            members = ", ".join(sorted(scc))
            c.warning(
                "unguarded-cycle",
                f"event cycle with no guard among activations: {members}",
                playbook.name,
            )


def _strongly_connected_cycles(edges: dict[str, set[str]]) -> list[set[str]]:
    """Return the node sets of SCCs that form a cycle (size>1, or a self-loop)."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    result: list[set[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal counter
        # Iterative Tarjan to stay safe on deep graphs.
        work: list[tuple[str, int]] = [(v, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            neighbours = sorted(edges[node])
            if pi < len(neighbours):
                work[-1] = (node, pi + 1)
                nxt = neighbours[pi]
                if nxt not in index_of:
                    work.append((nxt, 0))
                    recursed = True
                elif nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if recursed:
                continue
            if pi >= len(neighbours):
                if low[node] == index_of[node]:
                    comp: set[str] = set()
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.add(w)
                        if w == node:
                            break
                    is_cycle = len(comp) > 1 or node in edges[node]
                    if is_cycle:
                        result.append(comp)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])

    for v in edges:
        if v not in index_of:
            strongconnect(v)
    return result


# --------------------------------------------------------------------------- #
# Determinism AST rule (D23) — scan ``impl`` module source.
# --------------------------------------------------------------------------- #


def scan_impl_source(source: str, location: str = "") -> list[Diagnostic]:
    """Scan ``impl`` Python source for nondeterminism (D23), returning findings.

    Flags wall-clock reads (``datetime.now``, ``time.time`` …), unseeded module-
    level randomness (``random.random`` and friends, ``uuid.uuid4``, ``os.urandom``,
    any ``secrets`` call), each as an ``"nondeterminism"`` **error**. Calls on a
    *seeded* ``random.Random`` instance are fine — only attribute calls on the bare
    stdlib ``random`` module (or the other sources) are flagged. A syntax error in
    the source is itself reported (``"impl-syntax-error"``).

    The rule is purely syntactic, so it never imports or executes the module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            Diagnostic("impl-syntax-error", Severity.ERROR, f"cannot parse impl: {exc}", location)
        ]

    findings: list[Diagnostic] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        # The qualifier is the name immediately before the called attribute, so
        # both ``time.time()`` and ``datetime.datetime.now()`` resolve their module
        # to ``time`` / ``datetime``. A call on a *seeded* ``random.Random``
        # instance (``rng.random()``) has qualifier ``rng`` — not flagged.
        value = func.value
        if isinstance(value, ast.Name):
            qualifier = value.id
        elif isinstance(value, ast.Attribute):
            qualifier = value.attr
        else:
            continue
        if qualifier in _FORBIDDEN_MODULES or (qualifier, attr) in _FORBIDDEN_CALLS:
            findings.append(
                Diagnostic(
                    "nondeterminism",
                    Severity.ERROR,
                    f"forbidden nondeterministic call {qualifier}.{attr}() in impl code",
                    location,
                )
            )
    return findings


def _resolve_impl_source(impl: str) -> str | None:
    """Return the source of an ``impl`` ``"pkg.module:ClassName"`` ref, or ``None``.

    Locates the module via :func:`importlib.util.find_spec` *without importing it*
    and reads its source file. Returns ``None`` when the module can't be located
    (it may be authored separately and not yet on the path) — an unresolved ``impl``
    is not itself a Tier 1 error, so the determinism scan is simply skipped.
    """
    module_path = impl.split(":", 1)[0]
    try:
        spec = importlib.util.find_spec(module_path)
    except (ImportError, ValueError, ModuleNotFoundError):
        return None
    if spec is None or spec.origin is None:
        return None
    origin = Path(spec.origin)
    if not origin.is_file() or origin.suffix != ".py":
        return None
    try:
        return origin.read_text(encoding="utf-8")
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Public entry points.
# --------------------------------------------------------------------------- #


def lint_process(process: Process, *, namespace: str = "") -> LintResult:
    """Lint a single :class:`Process` in isolation (no event-graph checks).

    Runs the type/schema, reference-integrity, and static ``declares``-conformance
    checks. Event-graph soundness needs the surrounding activations, so it only
    runs in :func:`lint_playbook`.
    """
    c = _Collector()
    base = f"{namespace}/{process.name}" if namespace else process.name
    _check_process_refs(process, c, base)
    _check_declares_conformance(process, c, base)
    _scan_process_impl(process, c, base)
    return c.result()


def _scan_process_impl(process: Process, c: _Collector, base: str) -> None:
    """Apply the determinism AST scan to a process's ``impl`` module, if resolvable."""
    if process.impl is None:
        return
    source = _resolve_impl_source(process.impl)
    if source is None:
        return
    c.extend(scan_impl_source(source, location=f"{base} (impl {process.impl})"))


def lint_playbook(playbook: Playbook) -> LintResult:
    """Lint a whole :class:`Playbook`: per-process checks plus event-graph soundness.

    Each distinct process is checked once (the type/schema, reference-integrity,
    ``declares``-conformance, and determinism rules); then the activation graph is
    checked for dead triggers, unreachable processes, and unguarded cycles.
    """
    c = _Collector()
    base = playbook.name

    # Activation id uniqueness.
    seen_acts: set[str] = set()
    for act in playbook.activations:
        loc = f"{base}/{act.id}"
        if act.id in seen_acts:
            c.error("duplicate-activation", f"duplicate activation id {act.id!r}", base)
        seen_acts.add(act.id)
        _check_trigger_schema(act, c, loc)
        _check_volume(act, c, loc)

    # Per-process checks — once per distinct process object (by name).
    seen_processes: set[str] = set()
    for act in playbook.activations:
        proc = act.process
        if proc.name in seen_processes:
            continue
        seen_processes.add(proc.name)
        pbase = f"{base}/{proc.name}"
        _check_process_refs(proc, c, pbase)
        _check_declares_conformance(proc, c, pbase)
        _scan_process_impl(proc, c, pbase)

    # Playbook-level deliverable expectations (P4): every expected kind should be
    # produced (declared) by some process.
    declared_kinds: set[str] = set()
    for act in playbook.activations:
        declared_kinds |= set(act.process.declares.deliverables)
    for kind in playbook.deliverable_expectations:
        if kind not in declared_kinds:
            c.warning(
                "unmet-expectation",
                f"deliverable_expectation {kind!r} is produced by no process",
                base,
            )

    _check_event_graph(playbook, c)
    return c.result()


def format_result(result: LintResult, target: str) -> str:
    """Render a :class:`LintResult` as human-readable lines for the CLI."""
    if not result.diagnostics:
        return f"{target}: clean (0 diagnostics)"
    lines = [f"{target}: {len(result.errors())} error(s), {len(result.warnings())} warning(s)"]
    lines.extend(f"  {d}" for d in result.diagnostics)
    return "\n".join(lines)
