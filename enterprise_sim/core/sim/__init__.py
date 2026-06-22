"""Clock, event queue/scheduler, actor/relationship resolver.

The actor/relationship resolver (ARCHITECTURE §15.3, D28) binds a
:class:`Selector` to concrete people and writes the relationship edges it
implies into the KG. See :mod:`enterprise_sim.core.sim.resolver`.

The deterministic discrete-event core (ARCHITECTURE.md §15): a monotonic
:class:`Clock`, a stable-ordered :class:`EventQueue` keyed by :class:`ScheduleKey`
(D26), and a v1 :class:`WorkingCalendar` for working-time arithmetic (D27).
"""

from __future__ import annotations

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

__all__ = [
    "Clock",
    "EventQueue",
    "Filter",
    "FilterOp",
    "RankSignal",
    "RankWeights",
    "Resolution",
    "Resolver",
    "ScheduleKey",
    "Selector",
    "WorkingCalendar",
]
