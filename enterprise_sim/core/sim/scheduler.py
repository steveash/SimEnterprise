"""The deterministic discrete-event scheduler (ARCHITECTURE §15.1/§15.4, D26/D27).

This is Layer B's engine: it **drives time and fires the six triggers**, turning a
:class:`~enterprise_sim.core.sim.spec.Scenario` (activations wiring processes to
triggers) plus a populated :class:`~enterprise_sim.core.world.World` into a
**fully-ordered event log** with the KG mutations and per-person calendars it
implies (§15.4). It is the most direct determinant of corpus realism and the thing
conformance invariants I1–I4 assert against.

The loop is the textbook discrete-event one (§15.1): a min-heap
:class:`~enterprise_sim.core.sim.event_queue.EventQueue` of future firings keyed by
a stable :class:`~enterprise_sim.core.sim.event_queue.ScheduleKey`; pop the
earliest, execute it (which emits events, mutates the KG, and may enqueue more),
advance the :class:`~enterprise_sim.core.sim.clock.Clock`. Each trigger maps onto
that loop (§15.1):

* **OnStart / OnCadence / Probabilistic** are *seeded* — all their firings are
  computed up front (cadence by rule, ``Probabilistic`` pre-sampled from a seeded
  Poisson process) and pushed before the loop runs.
* **OnEvent** is *reactive* — every emitted event is matched against ``OnEvent``
  subscriptions and enqueues firings; this is also how gates and cascades run.
* **OnMilestone** fires on a ``MILESTONE`` effect; **OnCondition** subscribes to
  the KG attributes its predicate reads and re-evaluates the instant a ``MUTATE``
  effect touches one (effect-driven), with a coarse daily safety tick for purely
  time-based predicates.

**Determinism (D26).** Order never depends on the insertion race: items pop by the
total key ``(timestamp, process_priority, instance_id, step_id)``; every stochastic
draw (role resolution, comment counts, comment placement, probabilistic arrivals)
pulls from a sub-stream seeded by ``(root, scenario, activation, instance, …)``.
Two runs from the same root seed therefore emit a byte-identical ordered log. A
runaway-cycle backstop (``max_events``) and a forced-overlap flag surface as
:class:`ValidationIssue` s rather than corrupting the log.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from enterprise_sim.core.config.seed import substream
from enterprise_sim.core.events import Event, EventJournal
from enterprise_sim.core.sim.busy_map import BusyMap
from enterprise_sim.core.sim.calendar import WorkingCalendar
from enterprise_sim.core.sim.clock import Clock
from enterprise_sim.core.sim.event_queue import EventQueue, ScheduleKey
from enterprise_sim.core.sim.resolver import Resolver
from enterprise_sim.core.sim.spec import (
    Activation,
    EffectKind,
    OnCadence,
    OnCondition,
    OnEvent,
    OnMilestone,
    OnStart,
    Probabilistic,
    Scenario,
    Step,
    parse_business_days,
    parse_duration_hours,
    parse_int_range,
)
from enterprise_sim.core.world import Edge, Node, World

__all__ = ["Scheduler", "ScheduleResult", "ValidationIssue"]

# Default working-time a slot occupies when a step / comment does not say.
_DEFAULT_SLOT = timedelta(minutes=30)
_DEFAULT_COMMENT_SLOT = timedelta(minutes=15)
# Backstop against an unguarded trigger cycle (§13 "unguarded cycles").
_DEFAULT_MAX_EVENTS = 100_000
# Priority sentinel so daily condition ticks settle after real firings at an instant.
_TICK_PRIORITY = 1_000_000_000

_WEEKDAYS: dict[str, int] = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}
_PERIOD_DAYS: dict[str, float] = {"day": 1.0, "week": 7.0, "sprint": 14.0, "month": 30.0}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A soft problem the scheduler chose to log rather than fail on (D17).

    Attributes:
        code: Stable machine code (``forced_overlap``, ``max_events`` …).
        message: Human-readable detail.
        at: Sim-time the issue arose, if applicable.
        subject: Related node/event id, if applicable.
    """

    code: str
    message: str
    at: datetime | None = None
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """The frozen output of a run (§15.4): the log plus what it implied.

    Attributes:
        journal: The fully-ordered, deterministic event log (the canonical output
            Layer C renders and Layer D exports).
        world: The KG after all mutations — created nodes, relationship/affinity
            edges (resolver), and derived per-person ``CalendarEvent`` nodes.
        busy_map: The per-person busy map the placement filled.
        issues: Soft validation issues logged during the run.
    """

    journal: EventJournal
    world: World
    busy_map: BusyMap
    issues: tuple[ValidationIssue, ...] = ()


# --------------------------------------------------------------------------- #
# Internal queue payloads.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Firing:
    """A queued process-instance firing."""

    activation: Activation
    at: datetime
    instance_id: str


@dataclass(frozen=True, slots=True)
class _ConditionTick:
    """A queued daily safety tick re-evaluating time-based ``OnCondition`` predicates."""

    at: datetime


@dataclass(slots=True)
class _RunState:
    """Mutable bookkeeping threaded through one :meth:`Scheduler.run`."""

    scenario: Scenario
    start: datetime
    end: datetime
    max_events: int
    queue: EventQueue[object]
    journal: EventJournal
    busy: BusyMap
    clock: Clock
    issues: list[ValidationIssue] = field(default_factory=list)
    # instance ids already executed or already enqueued (push/run dedup).
    fired_instances: set[str] = field(default_factory=set)
    scheduled: set[str] = field(default_factory=set)
    # OnCondition activations that have fired (fire-once gate semantics).
    condition_fired: set[str] = field(default_factory=set)
    stopped: bool = False


# --------------------------------------------------------------------------- #
# Seeded firing computation (OnCadence / Probabilistic).
# --------------------------------------------------------------------------- #


def cadence_firings(
    rule: str, start: datetime, end: datetime, calendar: WorkingCalendar
) -> list[datetime]:
    """Return all firing instants for an ``OnCadence`` ``rule`` within ``[start, end]``.

    Supported rules (§12.1): ``daily:workdays`` (every working day at the day's
    open), ``weekly:DOW`` (``DOW`` ∈ MON..SUN, on that weekday when it is a working
    day), and ``every:Nw`` / ``every:Nd`` / ``per_sprint:Nw`` (every ``N`` weeks or
    days from ``start``, snapped forward to the next working start). All firings
    land on working time and are returned sorted.
    """
    kind, _, arg = rule.partition(":")
    kind = kind.strip().lower()
    arg = arg.strip().upper()
    fires: list[datetime] = []

    if kind == "daily":
        day = start.date()
        while day <= end.date():
            if day.weekday() in calendar.working_weekdays:
                fire = datetime.combine(day, calendar.day_start, tzinfo=start.tzinfo)
                if start <= fire <= end:
                    fires.append(fire)
            day += timedelta(days=1)
        return fires

    if kind == "weekly":
        if arg not in _WEEKDAYS:
            raise ValueError(f"unknown weekday in cadence rule: {rule!r}")
        target = _WEEKDAYS[arg]
        day = start.date()
        while day <= end.date():
            if day.weekday() == target and day.weekday() in calendar.working_weekdays:
                fire = datetime.combine(day, calendar.day_start, tzinfo=start.tzinfo)
                if start <= fire <= end:
                    fires.append(fire)
            day += timedelta(days=1)
        return fires

    if kind in ("every", "per_sprint"):
        token = arg if kind == "every" else (arg if arg.endswith(("W", "D")) else arg + "W")
        if kind == "per_sprint" and not arg:
            token = "2W"
        unit = token[-1]
        try:
            n = int(token[:-1])
        except ValueError as exc:
            raise ValueError(f"malformed cadence interval: {rule!r}") from exc
        if n <= 0:
            raise ValueError(f"cadence interval must be positive: {rule!r}")
        step_days = n * 7 if unit == "W" else n
        cursor = start
        seen: set[datetime] = set()
        while cursor <= end:
            fire = calendar.next_working_start(cursor)
            if fire <= end and fire not in seen:
                seen.add(fire)
                fires.append(fire)
            cursor += timedelta(days=step_days)
        return sorted(fires)

    raise ValueError(f"unknown cadence rule: {rule!r}")


def probabilistic_firings(
    trigger: Probabilistic,
    start: datetime,
    end: datetime,
    calendar: WorkingCalendar,
    rng: random.Random,
) -> list[datetime]:
    """Pre-sample ``trigger``'s firings over ``[start, end]`` from a seeded RNG.

    A homogeneous Poisson process: inter-arrival gaps are exponential with mean
    ``period / rate`` calendar-days, accumulated from ``start`` and each arrival
    snapped to the next working start (§15.1). Because the whole sequence is drawn
    up front from ``rng``, the firings are fixed once the seed is.
    """
    if trigger.rate <= 0:
        return []
    mean_days = _PERIOD_DAYS[trigger.per] / trigger.rate
    fires: list[datetime] = []
    cursor = start
    while True:
        gap = rng.expovariate(1.0 / mean_days)
        cursor = cursor + timedelta(days=gap)
        if cursor > end:
            break
        fires.append(calendar.next_working_start(cursor))
    return fires


# --------------------------------------------------------------------------- #
# The scheduler.
# --------------------------------------------------------------------------- #


class Scheduler:
    """Drives a scenario to a fully-ordered event log over a populated world.

    Construct over the KG (already holding the people Layer A generated) and a
    working calendar; :meth:`run` then plays a scenario across a window. The
    resolver is built over the same world so the relationship/affinity edges it
    writes accumulate in the returned graph (§15.3).
    """

    def __init__(
        self,
        world: World,
        calendar: WorkingCalendar,
        *,
        root_seed: int,
        resolver: Resolver | None = None,
    ) -> None:
        """Construct a scheduler.

        Args:
            world: The KG to read people from and write mutations into.
            calendar: Working-time calendar for all placement arithmetic (D27).
            root_seed: Run root seed; every sub-stream derives from it (D26).
            resolver: Actor/relationship resolver; one is built over ``world`` with
                default knobs when omitted.
        """
        self._world = world
        self._calendar = calendar
        self._root_seed = root_seed
        self._resolver = resolver if resolver is not None else Resolver(world)

    # -- public API ---------------------------------------------------------

    def run(
        self,
        scenario: Scenario,
        *,
        start: datetime,
        end: datetime,
        max_events: int = _DEFAULT_MAX_EVENTS,
    ) -> ScheduleResult:
        """Play ``scenario`` across ``[start, end]`` and return the ordered log.

        Args:
            scenario: The activations to run (the triggering graph).
            start: Inclusive window start (the OnStart instant; cadence/prob seed
                from here).
            end: Inclusive window end; no firing past it is scheduled.
            max_events: Runaway-cycle backstop — once the log exceeds this, the run
                stops and logs a ``max_events`` issue (§13).

        Returns:
            A :class:`ScheduleResult` with the journal, mutated world, busy map,
            and any validation issues.
        """
        if end < start:
            raise ValueError(f"end ({end.isoformat()}) precedes start ({start.isoformat()})")

        state = _RunState(
            scenario=scenario,
            start=start,
            end=end,
            max_events=max_events,
            queue=EventQueue(),
            journal=EventJournal(),
            busy=BusyMap(self._calendar),
            clock=Clock(start),
        )

        self._seed_queue(state)
        self._drain(state)
        self._materialize_calendars(state)

        return ScheduleResult(
            journal=state.journal,
            world=self._world,
            busy_map=state.busy,
            issues=tuple(state.issues),
        )

    # -- queue seeding ------------------------------------------------------

    def _seed_queue(self, state: _RunState) -> None:
        """Push the up-front firings (OnStart / OnCadence / Probabilistic) + ticks."""
        has_condition = False
        for act in state.scenario.activations:
            trigger = act.trigger
            if isinstance(trigger, OnStart):
                fire = self._calendar.next_working_start(state.start)
                if fire <= state.end:
                    self._enqueue_firing(state, act, fire, f"{act.id}@start")
            elif isinstance(trigger, OnCadence):
                for fire in cadence_firings(trigger.rule, state.start, state.end, self._calendar):
                    self._enqueue_firing(state, act, fire, f"{act.id}@{fire.isoformat()}")
            elif isinstance(trigger, Probabilistic):
                rng = substream(self._root_seed, state.scenario.name, act.id, "probabilistic")
                for fire in probabilistic_firings(
                    trigger, state.start, state.end, self._calendar, rng
                ):
                    self._enqueue_firing(state, act, fire, f"{act.id}@{fire.isoformat()}")
            elif isinstance(trigger, OnCondition):
                has_condition = True
        if has_condition:
            self._seed_condition_ticks(state)

    def _seed_condition_ticks(self, state: _RunState) -> None:
        """Push a daily safety tick at each working day's open (§15.1 coarse tick)."""
        day = state.start.date()
        while day <= state.end.date():
            if day.weekday() in self._calendar.working_weekdays:
                tick = datetime.combine(day, self._calendar.day_start, tzinfo=state.start.tzinfo)
                if state.start <= tick <= state.end:
                    state.queue.push(
                        ScheduleKey(tick, _TICK_PRIORITY, "__tick__", tick.isoformat()),
                        _ConditionTick(tick),
                    )
            day += timedelta(days=1)

    def _enqueue_firing(
        self, state: _RunState, act: Activation, at: datetime, instance_id: str
    ) -> None:
        """Push one firing, skipping anything already scheduled or run (push dedup)."""
        if instance_id in state.scheduled or instance_id in state.fired_instances:
            return
        if at > state.end:
            return
        state.scheduled.add(instance_id)
        key = ScheduleKey(at, act.process.priority, instance_id, "")
        state.queue.push(key, _Firing(act, at, instance_id))

    # -- main loop ----------------------------------------------------------

    def _drain(self, state: _RunState) -> None:
        """Pop items in stable key order until the queue empties or the cap trips."""
        while state.queue and not state.stopped:
            key, item = state.queue.pop()
            state.clock.advance_to(key.timestamp)
            if isinstance(item, _ConditionTick):
                self._on_tick(state, item.at)
            elif isinstance(item, _Firing):
                if item.instance_id in state.fired_instances:
                    continue
                state.fired_instances.add(item.instance_id)
                self._run_instance(state, item.activation, item.at, item.instance_id)
            if len(state.journal) > state.max_events:
                state.issues.append(
                    ValidationIssue(
                        "max_events",
                        f"event cap {state.max_events} exceeded; possible unguarded cycle",
                        at=key.timestamp,
                    )
                )
                state.stopped = True

    def _on_tick(self, state: _RunState, at: datetime) -> None:
        """Daily tick: fire any ``OnCondition`` whose predicate now holds."""
        for act in state.scenario.activations:
            trigger = act.trigger
            if not isinstance(trigger, OnCondition):
                continue
            if act.id in state.condition_fired:
                continue
            if trigger.condition.evaluate(self._world):
                self._fire_condition(state, act, at)

    # -- instance execution -------------------------------------------------

    def _run_instance(
        self, state: _RunState, act: Activation, at: datetime, instance_id: str
    ) -> None:
        """Execute one process instance: resolve roles, place steps, emit, mutate."""
        roles = self._resolve_roles(state, act, instance_id, at)
        step_end: dict[str, datetime] = {}
        step_event_id: dict[str, str] = {}

        for step in act.process.steps:
            base = self._step_base_time(step, at, step_end)
            # Bound the corpus to the window: once a step's anchor falls past the
            # window end, this instance's remaining (later) steps are out of scope.
            if self._calendar.next_working_start(base) > state.end:
                break
            event_id = f"evt:{instance_id}:{step.id}"
            actors = roles.get(step.by) or [] if step.by else []
            event_time = self._place_step(state, step, base, actors, event_id)

            created = self._apply_effects(state, step, act, event_time)
            self._emit_step_event(
                state, act, step, roles, event_id, event_time, step_event_id, created
            )

            if step.spread is not None:
                self._spread_comments(state, act, step, roles, event_id, event_time, instance_id)

            work_hours = self._step_work_hours(step)
            step_end[step.id] = self._calendar.advance(event_time, work_hours)
            step_event_id[step.id] = event_id

    def _resolve_roles(
        self, state: _RunState, act: Activation, instance_id: str, at: datetime
    ) -> dict[str, list[str]]:
        """Bind every process role to node ids (explicit binds or resolver draws)."""
        roles: dict[str, list[str]] = {}
        for role in act.process.roles:
            if role.name in act.bind:
                roles[role.name] = list(act.bind[role.name])
                continue
            if role.selector is not None:
                rng = substream(
                    self._root_seed,
                    state.scenario.name,
                    act.id,
                    instance_id,
                    "role",
                    role.name,
                )
                resolution = self._resolver.resolve(
                    role.selector,
                    rng=rng,
                    at=at,
                    anchor=act.anchor,
                    relationship=role.relationship,
                )
                roles[role.name] = list(resolution.ids)
            else:
                roles[role.name] = []
        return roles

    def _step_base_time(
        self, step: Step, instance_start: datetime, step_end: dict[str, datetime]
    ) -> datetime:
        """Compute a step's anchor instant from its ``at`` / ``after`` + ``offset``."""
        if step.after is not None:
            if step.after not in step_end:
                raise ValueError(f"step {step.id!r} follows unknown step {step.after!r}")
            base = step_end[step.after]
        else:
            days = parse_business_days(step.at) if step.at is not None else 0
            base = self._calendar.advance(instance_start, days * self._calendar.hours_per_day)
        if step.offset is not None:
            base = self._calendar.advance(
                base, parse_duration_hours(step.offset, self._calendar.hours_per_day)
            )
        return base

    def _place_step(
        self,
        state: _RunState,
        step: Step,
        base: datetime,
        actors: list[str],
        event_id: str,
    ) -> datetime:
        """Greedily book the acting people and return the step's event time.

        The first actor's booked slot fixes the event time; every other actor is
        booked their own non-overlapping slot near ``base`` so the busy map stays
        consistent (§15.2). A step with no actors fires at the next working start.
        """
        slot = self._slot_duration(step)
        if not actors:
            return self._calendar.next_working_start(base)
        first_start: datetime | None = None
        for person in actors:
            start, forced = state.busy.book(
                person, base, slot, kind=step.emits, source_event=event_id
            )
            if forced:
                state.issues.append(
                    ValidationIssue(
                        "forced_overlap",
                        f"forced overlapping slot for {person} on {step.emits}",
                        at=start,
                        subject=person,
                    )
                )
            if first_start is None:
                first_start = start
        assert first_start is not None
        return first_start

    def _emit_step_event(
        self,
        state: _RunState,
        act: Activation,
        step: Step,
        roles: dict[str, list[str]],
        event_id: str,
        event_time: datetime,
        step_event_id: dict[str, str],
        created: list[str],
    ) -> None:
        """Append the step's event to the log and dispatch it to reactive triggers."""
        parent_key = step.parent_step or step.after
        parent_event = step_event_id.get(parent_key) if parent_key is not None else None
        subjects: list[str] = []
        if act.anchor is not None:
            subjects.append(act.anchor)
        subjects.extend(created)
        event = Event(
            id=event_id,
            type=step.emits,
            timestamp=event_time,
            actors={name: list(ids) for name, ids in roles.items() if ids},
            initiative=act.params.get("initiative"),
            project=act.params.get("project", act.anchor),
            subjects=subjects,
            deliverable=step.produces,
            parent_event=parent_event,
            payload={**dict(act.params), **dict(step.payload)},
        )
        state.journal.append(event)
        self._dispatch_event(state, event)

    def _spread_comments(
        self,
        state: _RunState,
        act: Activation,
        step: Step,
        roles: dict[str, list[str]],
        parent_event_id: str,
        window_start: datetime,
        instance_id: str,
    ) -> None:
        """Distribute threaded comments across the step window (§15.2 threading).

        Each actor in the spread role posts a seeded number of comments at seeded
        offsets within the working-time window, booked around their busy slots. A
        comment threads to an *earlier* comment (with probability ``reply_rate``)
        or to the step's parent event — always an already-emitted event, so threads
        are well-formed (I4) and temporally sane.
        """
        spread = step.spread
        assert spread is not None
        window_hours = self._spread_window_hours(step)
        lo, hi = parse_int_range(spread.per_actor)
        # (comment_id, timestamp) for every comment posted so far in this window.
        posted: list[tuple[str, datetime]] = []

        for person in roles.get(spread.role) or []:
            rng = substream(
                self._root_seed,
                state.scenario.name,
                act.id,
                instance_id,
                "spread",
                step.id,
                person,
            )
            k = lo if lo == hi else rng.randint(lo, hi)
            for n in range(k):
                fraction = rng.random()
                earliest = self._calendar.advance(window_start, fraction * window_hours)
                if self._calendar.next_working_start(earliest) > state.end:
                    continue  # comment would fall outside the window
                start, forced = state.busy.book(
                    person,
                    earliest,
                    _DEFAULT_COMMENT_SLOT,
                    kind=spread.emits,
                    source_event=f"{parent_event_id}:c:{person}:{n}",
                )
                if forced:
                    state.issues.append(
                        ValidationIssue(
                            "forced_overlap",
                            f"forced overlapping comment slot for {person}",
                            at=start,
                            subject=person,
                        )
                    )
                comment_id = f"{parent_event_id}:c:{person}:{n}"
                parent = self._pick_thread_parent(
                    posted, start, parent_event_id, rng, spread.reply_rate
                )
                comment = Event(
                    id=comment_id,
                    type=spread.emits,
                    timestamp=start,
                    actors={spread.role: [person]},
                    initiative=act.params.get("initiative"),
                    project=act.params.get("project", act.anchor),
                    subjects=[act.anchor] if act.anchor is not None else [],
                    parent_event=parent,
                    payload={**dict(act.params), "in_reply_to": parent},
                )
                state.journal.append(comment)
                posted.append((comment_id, start))
                self._dispatch_event(state, comment)

    @staticmethod
    def _pick_thread_parent(
        posted: list[tuple[str, datetime]],
        at: datetime,
        default_parent: str,
        rng: random.Random,
        reply_rate: float,
    ) -> str:
        """Choose a comment's parent: an earlier comment or the default (the draft).

        Candidates are the comments already posted with a strictly earlier
        timestamp; one is chosen with probability ``reply_rate`` (seeded, over
        ids sorted for determinism), else the step's parent event is used.
        """
        candidates = sorted(cid for cid, ts in posted if ts < at)
        if candidates and rng.random() < reply_rate:
            return candidates[rng.randrange(len(candidates))]
        return default_parent

    # -- effects + reactive dispatch ---------------------------------------

    def _apply_effects(
        self, state: _RunState, step: Step, act: Activation, at: datetime
    ) -> list[str]:
        """Apply a step's KG effects; return ids of nodes it created.

        ``MUTATE`` effects re-evaluate any ``OnCondition`` watching the touched
        attribute (effect-driven path); ``MILESTONE`` effects fire subscribed
        ``OnMilestone`` activations.
        """
        created: list[str] = []
        for effect in step.effects:
            if effect.kind is EffectKind.CREATE_NODE:
                if self._world.get_node(effect.target) is None:
                    self._world.add_node(
                        Node(effect.target, effect.node_type, at, props=dict(effect.props))
                    )
                created.append(effect.target)
            elif effect.kind is EffectKind.MUTATE:
                node = self._world.get_node(effect.target)
                if node is not None:
                    node.props[effect.attr] = effect.value
                self._dispatch_condition_change(state, effect.attr, at)
            elif effect.kind is EffectKind.ADD_EDGE:
                if self._world.get_edge(effect.target) is None:
                    self._world.add_edge(
                        Edge(effect.target, effect.edge_type, effect.src, effect.dst, at)
                    )
            elif effect.kind is EffectKind.MILESTONE:
                self._dispatch_milestone(state, effect.name, at)
        return created

    def _dispatch_event(self, state: _RunState, event: Event) -> None:
        """Enqueue reactive firings for every ``OnEvent`` subscription this matches."""
        fields = _event_fields(event)
        for act in state.scenario.activations:
            trigger = act.trigger
            if not isinstance(trigger, OnEvent):
                continue
            if trigger.event_type != event.type:
                continue
            if not all(pred.matches(fields) for pred in trigger.where):
                continue
            self._enqueue_firing(state, act, event.timestamp, f"{act.id}@evt@{event.id}")

    def _dispatch_milestone(self, state: _RunState, name: str, at: datetime) -> None:
        """Enqueue reactive firings for ``OnMilestone`` activations matching ``name``."""
        for act in state.scenario.activations:
            trigger = act.trigger
            if isinstance(trigger, OnMilestone) and trigger.name == name:
                self._enqueue_firing(state, act, at, f"{act.id}@ms@{name}@{at.isoformat()}")

    def _dispatch_condition_change(self, state: _RunState, attr: str, at: datetime) -> None:
        """Re-evaluate ``OnCondition`` activations reading ``attr`` (effect-driven)."""
        for act in state.scenario.activations:
            trigger = act.trigger
            if not isinstance(trigger, OnCondition):
                continue
            if act.id in state.condition_fired:
                continue
            if attr not in trigger.condition.watched_attrs():
                continue
            if trigger.condition.evaluate(self._world):
                self._fire_condition(state, act, at)

    def _fire_condition(self, state: _RunState, act: Activation, at: datetime) -> None:
        """Mark an ``OnCondition`` fired-once and enqueue its instance."""
        state.condition_fired.add(act.id)
        self._enqueue_firing(state, act, at, f"{act.id}@cond")

    # -- derived calendars --------------------------------------------------

    def _materialize_calendars(self, state: _RunState) -> None:
        """Derive per-person ``CalendarEvent`` nodes from the busy map (§15.4).

        Each booking becomes a ``CalendarEvent`` node (stamped at its start) linked
        from its person by a ``has_calendar_event`` edge — "per-person calendars
        derive from the busy map" — giving Layer D real, non-overlapping calendars.
        """
        for booking in state.busy.all_bookings():
            cal_id = f"cal:{booking.source_event}:{booking.person}"
            if self._world.get_node(cal_id) is not None:
                continue
            self._world.add_node(
                Node(
                    cal_id,
                    "CalendarEvent",
                    booking.start,
                    props={
                        "person": booking.person,
                        "start": booking.start.isoformat(),
                        "end": booking.end.isoformat(),
                        "kind": booking.kind,
                        "source_event": booking.source_event,
                    },
                )
            )
            edge_id = f"edge:has_calendar_event:{booking.person}->{cal_id}"
            if self._world.get_edge(edge_id) is None:
                self._world.add_edge(
                    Edge(
                        edge_id,
                        "has_calendar_event",
                        booking.person,
                        cal_id,
                        booking.start,
                    )
                )

    # -- duration helpers ---------------------------------------------------

    def _slot_duration(self, step: Step) -> timedelta:
        """The busy-map slot a step occupies (small by default — never a whole day)."""
        if step.slot is None:
            return _DEFAULT_SLOT
        hours = parse_duration_hours(step.slot, self._calendar.hours_per_day)
        return timedelta(hours=hours)

    def _step_work_hours(self, step: Step) -> float:
        """Working-hours a step spans, for chaining ``after`` dependents."""
        if step.duration is not None:
            return parse_duration_hours(step.duration, self._calendar.hours_per_day)
        return self._slot_duration(step).total_seconds() / 3600.0

    def _spread_window_hours(self, step: Step) -> float:
        """Working-hours the spread distributes comments over (a day if unspecified)."""
        if step.duration is not None:
            return parse_duration_hours(step.duration, self._calendar.hours_per_day)
        return self._calendar.hours_per_day


def _event_fields(event: Event) -> dict[str, object]:
    """Flatten an event for ``EventPredicate`` matching (``type``/``project``/``payload.*``)."""
    fields: dict[str, object] = {
        "type": event.type,
        "project": event.project,
        "initiative": event.initiative,
    }
    for key, value in event.payload.items():
        fields[f"payload.{key}"] = value
    return fields
