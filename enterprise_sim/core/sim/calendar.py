"""The :class:`WorkingCalendar` — a v1 business-hour weekday calendar (D11/D27).

Step *durations* in the simulator are measured in **working time**, not wall-clock
time (ARCHITECTURE.md §15.2): a "2 business-hour" draft started Friday at 16:00
does not finish at 18:00 Friday — it finishes Monday at 10:00, because evenings
and weekends are not working time. The calendar is the single source of truth for
that arithmetic. It answers three questions the scheduler and the greedy step
placer need:

* :meth:`is_working` — is this instant inside a working window?
* :meth:`advance` — what instant is *N working hours* after this one?
* :meth:`next_free_slot` — where is the earliest working slot of a given length
  that does not collide with a person's already-booked (busy) intervals?

v1 is deliberately coarse (D27): a fixed local ``day_start``–``day_end`` window on
a fixed set of weekdays. Per-person timezones and holidays come later; the API is
shaped to absorb them. Datetimes are treated as *local wall-clock* — only their
weekday and time-of-day fields are inspected — so the calendar is tz-agnostic as
long as a caller stays in one timezone (``tzinfo`` is preserved through every
returned datetime).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

# Monday..Sunday as produced by :meth:`datetime.date.weekday` (Mon == 0).
_DEFAULT_WEEKDAYS: frozenset[int] = frozenset({0, 1, 2, 3, 4})
_DEFAULT_DAY_START: time = time(9, 0)
_DEFAULT_DAY_END: time = time(17, 0)


@dataclass(frozen=True, slots=True)
class WorkingCalendar:
    """A weekday, fixed-business-hours working calendar.

    Args:
        day_start: Inclusive start-of-day working time (default 09:00).
        day_end: Exclusive end-of-day working time (default 17:00). Must be
            strictly after ``day_start``.
        working_weekdays: Weekdays that are working days, using
            :meth:`datetime.date.weekday` numbering (Mon == 0). Defaults to
            Monday–Friday.

    Frozen so a calendar can be shared across the run without any caller mutating
    it, and so it can be snapshotted alongside the config for reproducibility.
    """

    day_start: time = _DEFAULT_DAY_START
    day_end: time = _DEFAULT_DAY_END
    working_weekdays: frozenset[int] = field(default=_DEFAULT_WEEKDAYS)

    def __post_init__(self) -> None:
        if self.day_end <= self.day_start:
            raise ValueError(
                f"day_end ({self.day_end}) must be strictly after day_start ({self.day_start})"
            )
        if not self.working_weekdays:
            raise ValueError("working_weekdays must contain at least one weekday")
        if any(d < 0 or d > 6 for d in self.working_weekdays):
            raise ValueError("working_weekdays must be in range 0..6 (Mon..Sun)")

    @property
    def hours_per_day(self) -> float:
        """Length of one working day, in hours."""
        return self._day_length().total_seconds() / 3600.0

    def is_working(self, t: datetime) -> bool:
        """Return whether ``t`` falls inside a working window.

        ``True`` iff ``t`` is on a working weekday and its time-of-day is in
        ``[day_start, day_end)``. The end of the day is exclusive, so an instant
        exactly at ``day_end`` is *not* working (it is the boundary the next unit
        of work skips past).
        """
        if t.weekday() not in self.working_weekdays:
            return False
        return self.day_start <= t.time() < self.day_end

    def next_working_start(self, t: datetime) -> datetime:
        """Return the earliest working instant at or after ``t``.

        If ``t`` is already inside a working window it is returned unchanged;
        otherwise the start of the next working window is returned.
        """
        d = t.date()
        if self._is_working_day(d):
            window_start, window_end = self._window(d, t.tzinfo)
            if t < window_start:
                return window_start
            if t < window_end:
                return t
        # Past today's window (or today is not a working day): jump to the next
        # working day's opening time.
        d += timedelta(days=1)
        while not self._is_working_day(d):
            d += timedelta(days=1)
        return self._window(d, t.tzinfo)[0]

    def advance(self, t: datetime, business_hours: float) -> datetime:
        """Return the instant ``business_hours`` of *working time* after ``t``.

        Working time accrues only inside working windows; evenings and non-working
        days are skipped. ``business_hours`` may be fractional. Counting starts at
        ``t`` if it is already working, otherwise at the next working instant, so
        non-working slack before the work begins is never charged.

        ``business_hours == 0`` returns ``t`` unchanged (no snapping); a negative
        value is rejected.
        """
        if business_hours < 0:
            raise ValueError(f"business_hours must be non-negative, got {business_hours}")
        remaining = timedelta(hours=business_hours)
        if remaining == timedelta(0):
            return t
        pos = self.next_working_start(t)
        while True:
            _, window_end = self._window(pos.date(), pos.tzinfo)
            available = window_end - pos
            if available >= remaining:
                return pos + remaining
            remaining -= available
            # Land exactly on the window boundary, then snap to the next window.
            pos = self.next_working_start(window_end)

    def next_free_slot(
        self,
        earliest: datetime,
        duration: timedelta,
        busy: Iterable[tuple[datetime, datetime]] = (),
    ) -> datetime:
        """Return the earliest working start for a ``duration``-long free slot.

        Greedy soft-constraint placement (D27): find the first working instant at
        or after ``earliest`` such that ``[start, start + duration)`` fits inside a
        single working day and overlaps none of the ``busy`` intervals. On a
        collision the search jumps to the end of the blocking interval; when the
        slot would run past ``day_end`` it moves to the next working day. The slot
        is contiguous wall-clock time within one day, so ``duration`` may not
        exceed one working day.

        Args:
            earliest: No slot earlier than this is considered.
            duration: Required contiguous length; must be ``> 0`` and at most one
                working day.
            busy: Already-booked ``(start, end)`` intervals to avoid (half-open;
                touching at an endpoint does not count as a collision). Order does
                not matter.

        Returns:
            The chosen slot's start instant.
        """
        if duration <= timedelta(0):
            raise ValueError(f"duration must be positive, got {duration}")
        if duration > self._day_length():
            raise ValueError(
                f"duration ({duration}) exceeds one working day ({self._day_length()})"
            )
        booked = sorted(busy)
        pos = self.next_working_start(earliest)
        while True:
            _, window_end = self._window(pos.date(), pos.tzinfo)
            slot_end = pos + duration
            if slot_end > window_end:
                # Will not fit before the day closes; try the next working day.
                pos = self.next_working_start(window_end)
                continue
            conflict = self._first_overlap(pos, slot_end, booked)
            if conflict is not None:
                # Resume searching at the end of the blocking interval.
                pos = self.next_working_start(conflict[1])
                continue
            return pos

    @staticmethod
    def _first_overlap(
        start: datetime,
        end: datetime,
        booked: list[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime] | None:
        """Return the first interval in ``booked`` overlapping ``[start, end)``."""
        for interval in booked:
            b_start, b_end = interval
            if b_start >= end:
                # ``booked`` is sorted by start; nothing further can overlap.
                break
            if b_end > start:
                return interval
        return None

    def _is_working_day(self, d: date) -> bool:
        return d.weekday() in self.working_weekdays

    def _window(self, d: date, tzinfo: tzinfo | None) -> tuple[datetime, datetime]:
        """Return the ``(start, end)`` working window for date ``d``.

        ``tzinfo`` (taken from the datetime being queried) is attached to both
        bounds so the window stays comparable with — and as aware/naive as — its
        caller's input.
        """
        return (
            datetime.combine(d, self.day_start, tzinfo=tzinfo),
            datetime.combine(d, self.day_end, tzinfo=tzinfo),
        )

    def _day_length(self) -> timedelta:
        return datetime.combine(date.min, self.day_end) - datetime.combine(date.min, self.day_start)
