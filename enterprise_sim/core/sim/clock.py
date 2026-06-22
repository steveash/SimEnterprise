"""The simulation :class:`Clock` (ARCHITECTURE.md §15.1).

In an event-driven engine the clock is *pulled* forward by the queue: pop the
earliest scheduled item, then advance the clock to its firing time. The clock
therefore enforces one invariant — **time never runs backward**. Honouring that
is what lets the emitted event log be a monotonic, replayable record; a step that
tried to schedule into the past is a bug, and the clock surfaces it immediately
rather than letting it corrupt ordering downstream.
"""

from __future__ import annotations

from datetime import datetime


class Clock:
    """A monotonic simulation clock.

    The clock holds the current simulated instant and only ever moves forward.
    The scheduler advances it to each popped event's timestamp via
    :meth:`advance_to`; :meth:`now` reads the present instant.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    @property
    def now(self) -> datetime:
        """The current simulated instant."""
        return self._now

    def advance_to(self, t: datetime) -> None:
        """Advance the clock to ``t``.

        ``t`` may equal :meth:`now` (several items firing at the same instant is
        normal), but must not precede it.

        Raises:
            ValueError: If ``t`` is before the current time.
        """
        if t < self._now:
            raise ValueError(
                f"clock cannot move backward: now={self._now.isoformat()}, "
                f"requested={t.isoformat()}"
            )
        self._now = t
