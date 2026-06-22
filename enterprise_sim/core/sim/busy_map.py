"""Per-person busy map + greedy non-overlap booking (ARCHITECTURE §15.2, D27).

Step placement is a **greedy soft-constraint** problem, not a solver one (D27): as
meetings and authoring land, each person's **busy map** fills, and new work prefers
free slots — overlapping only when forced. This module owns that bookkeeping. It
holds each person's booked intervals, books new ones via the calendar's
:meth:`~enterprise_sim.core.sim.calendar.WorkingCalendar.next_free_slot` (so a slot
lands on working time and dodges existing bookings), and records *why* each slot
exists so per-person calendars can be derived from it — "per-person calendars
derive from the busy map" (§15.2/§15.4).

A booking can be *forced* to overlap only when a duration cannot fit before the
window closes; the booker surfaces that as a validation issue rather than silently
double-booking (the believable-calendars guarantee).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from enterprise_sim.core.sim.calendar import WorkingCalendar

__all__ = ["Booking", "BusyMap"]


@dataclass(frozen=True, slots=True)
class Booking:
    """One reserved interval on a person's calendar, with its provenance.

    Attributes:
        person: Node id of the person whose time is booked.
        start: Inclusive working-time start of the booked slot.
        end: Exclusive end of the slot.
        kind: What the slot is (the emitting event's type, e.g. ``CommentPosted``).
        source_event: Id of the event the booking was made for (calendar↔log link).
    """

    person: str
    start: datetime
    end: datetime
    kind: str
    source_event: str


class BusyMap:
    """Per-person booked intervals with greedy, calendar-aware booking.

    One instance is threaded through a whole schedule run; the scheduler books
    every acting slot into it so later steps see — and avoid — earlier ones.
    Intervals per person are kept sorted by start, which is what both the overlap
    search and the derived-calendar emission rely on.
    """

    def __init__(self, calendar: WorkingCalendar) -> None:
        self._calendar = calendar
        self._by_person: dict[str, list[Booking]] = {}

    def intervals(self, person: str) -> list[tuple[datetime, datetime]]:
        """Return ``person``'s booked ``(start, end)`` intervals, sorted by start."""
        return [(b.start, b.end) for b in self._by_person.get(person, ())]

    def bookings(self, person: str) -> list[Booking]:
        """Return ``person``'s :class:`Booking` records, sorted by start."""
        return list(self._by_person.get(person, ()))

    def all_bookings(self) -> list[Booking]:
        """Return every booking across all people, ordered by ``(person, start)``.

        The deterministic order makes this directly usable to materialise
        per-person ``CalendarEvent`` nodes (§15.4) with stable ids.
        """
        result: list[Booking] = []
        for person in sorted(self._by_person):
            result.extend(self._by_person[person])
        return result

    def people(self) -> list[str]:
        """Person ids that have at least one booking, sorted."""
        return sorted(self._by_person)

    def book(
        self,
        person: str,
        earliest: datetime,
        duration: timedelta,
        *,
        kind: str,
        source_event: str,
    ) -> tuple[datetime, bool]:
        """Greedily book ``duration`` for ``person`` at/after ``earliest``.

        Finds the earliest working slot that fits ``duration`` and overlaps none
        of the person's existing bookings (D27). If no such slot fits in a single
        working day (``duration`` longer than a day), the slot is *forced* at the
        next working start and flagged.

        Args:
            person: Whose calendar to book.
            earliest: No slot before this instant is considered.
            duration: Required contiguous working-time length.
            kind: Slot kind (recorded on the booking).
            source_event: Event id the booking is for (recorded on the booking).

        Returns:
            ``(start, forced)`` — the chosen start instant and whether the slot
            had to overlap an existing booking (a validation concern).
        """
        busy = self.intervals(person)
        forced = False
        try:
            start = self._calendar.next_free_slot(earliest, duration, busy)
        except ValueError:
            # Duration exceeds one working day (or is otherwise unplaceable as a
            # contiguous free slot): fall back to the next working start and flag
            # the forced overlap rather than failing the run.
            start = self._calendar.next_working_start(earliest)
            forced = True
        self._insert(Booking(person, start, start + duration, kind, source_event))
        return start, forced

    def _insert(self, booking: Booking) -> None:
        """Insert ``booking`` into the person's list, keeping it sorted by start."""
        slots = self._by_person.setdefault(booking.person, [])
        index = 0
        while index < len(slots) and slots[index].start <= booking.start:
            index += 1
        slots.insert(index, booking)

    @staticmethod
    def has_overlap(intervals: Iterable[tuple[datetime, datetime]]) -> bool:
        """Return whether any two intervals in ``intervals`` overlap (half-open).

        A test/utility helper for the non-overlap acceptance check: touching at an
        endpoint does not count.
        """
        ordered = sorted(intervals)
        for (a_start, a_end), (b_start, b_end) in zip(ordered, ordered[1:], strict=False):
            if b_start < a_end:
                return True
            _ = a_start, b_end
        return False
