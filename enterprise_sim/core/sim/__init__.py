"""Clock, event queue/scheduler, actor/relationship resolver.

The deterministic discrete-event core (ARCHITECTURE.md §15): a monotonic
:class:`Clock`, a stable-ordered :class:`EventQueue` keyed by :class:`ScheduleKey`
(D26), and a v1 :class:`WorkingCalendar` for working-time arithmetic (D27).

The actor/relationship resolver (ARCHITECTURE §15.3, D28) binds a
:class:`Selector` to concrete people and writes the relationship edges it
implies into the KG. See :mod:`enterprise_sim.core.sim.resolver`.

The :class:`Scheduler` (ARCHITECTURE §15.1/§15.4, D26/D27) drives time and fires
the six triggers, producing a fully-ordered event log plus the KG mutations and
per-person calendars it implies. See :mod:`enterprise_sim.core.sim.scheduler`.
"""

from __future__ import annotations

from enterprise_sim.core.sim.busy_map import BusyMap
from enterprise_sim.core.sim.calendar import WorkingCalendar
from enterprise_sim.core.sim.clock import Clock
from enterprise_sim.core.sim.event_queue import EventQueue, ScheduleKey
from enterprise_sim.core.sim.resolver import (
    Filter,
    FilterOp,
    RankSignal,
    RankWeights,
    Resolution,
    Resolver,
    Selector,
)
from enterprise_sim.core.sim.scheduler import Scheduler, ScheduleResult, ValidationIssue
from enterprise_sim.core.sim.spec import (
    Activation,
    Condition,
    Effect,
    EffectKind,
    OnCadence,
    OnCondition,
    OnEvent,
    OnMilestone,
    OnStart,
    Probabilistic,
    Process,
    Scenario,
    Spread,
    Step,
    Trigger,
)

__all__ = [
    "Activation",
    "BusyMap",
    "Clock",
    "Condition",
    "Effect",
    "EffectKind",
    "EventQueue",
    "Filter",
    "FilterOp",
    "OnCadence",
    "OnCondition",
    "OnEvent",
    "OnMilestone",
    "OnStart",
    "Probabilistic",
    "Process",
    "RankSignal",
    "RankWeights",
    "Resolution",
    "Resolver",
    "ScheduleKey",
    "ScheduleResult",
    "Scenario",
    "Scheduler",
    "Selector",
    "Spread",
    "Step",
    "Trigger",
    "ValidationIssue",
    "WorkingCalendar",
]
