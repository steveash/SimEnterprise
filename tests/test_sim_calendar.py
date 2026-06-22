"""Tests for the v1 WorkingCalendar (esim-f8a5960b).

Acceptance: calendar math — is_working windows, working-time advance across
nights/weekends, and greedy free-slot placement around busy intervals.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from enterprise_sim.core.sim import WorkingCalendar

# 2026-06-22 is a Monday; 2026-06-27/28 are Sat/Sun. Used throughout below.
_MON = 22
_FRI = 26
_SAT = 27
_SUN = 28


def _dt(day: int, hour: int = 9, minute: int = 0, *, tz: bool = False) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=UTC if tz else None)


def test_is_working_inside_window() -> None:
    cal = WorkingCalendar()
    assert cal.is_working(_dt(_MON, 9))
    assert cal.is_working(_dt(_MON, 12))
    assert cal.is_working(_dt(_MON, 16, 59))


def test_is_working_boundaries_half_open() -> None:
    cal = WorkingCalendar()
    # Start inclusive, end exclusive.
    assert cal.is_working(_dt(_MON, 9, 0))
    assert not cal.is_working(_dt(_MON, 8, 59))
    assert not cal.is_working(_dt(_MON, 17, 0))


def test_is_working_weekend() -> None:
    cal = WorkingCalendar()
    assert not cal.is_working(_dt(_SAT, 12))
    assert not cal.is_working(_dt(_SUN, 12))


def test_next_working_start_passthrough_when_working() -> None:
    cal = WorkingCalendar()
    t = _dt(_MON, 10, 30)
    assert cal.next_working_start(t) == t


def test_next_working_start_before_open_snaps_to_open() -> None:
    cal = WorkingCalendar()
    assert cal.next_working_start(_dt(_MON, 6)) == _dt(_MON, 9)


def test_next_working_start_after_close_snaps_to_next_day() -> None:
    cal = WorkingCalendar()
    # Monday evening -> Tuesday 09:00.
    assert cal.next_working_start(_dt(_MON, 20)) == _dt(_MON + 1, 9)


def test_next_working_start_skips_weekend() -> None:
    cal = WorkingCalendar()
    # Friday evening -> Monday 09:00.
    assert cal.next_working_start(_dt(_FRI, 18)) == _dt(_MON + 7, 9)


def test_advance_within_day() -> None:
    cal = WorkingCalendar()
    assert cal.advance(_dt(_MON, 9), 3) == _dt(_MON, 12)


def test_advance_fractional_hours() -> None:
    cal = WorkingCalendar()
    assert cal.advance(_dt(_MON, 9), 1.5) == _dt(_MON, 10, 30)


def test_advance_zero_is_identity() -> None:
    cal = WorkingCalendar()
    t = _dt(_MON, 13, 17)
    assert cal.advance(t, 0) == t


def test_advance_rolls_over_night() -> None:
    cal = WorkingCalendar()
    # 16:00 Monday + 2 business hours -> 1h Monday + 1h Tuesday morning.
    assert cal.advance(_dt(_MON, 16), 2) == _dt(_MON + 1, 10)


def test_advance_rolls_over_weekend() -> None:
    cal = WorkingCalendar()
    # Friday 16:00 + 2 business hours -> Monday 10:00.
    assert cal.advance(_dt(_FRI, 16), 2) == _dt(_MON + 7, 10)


def test_advance_from_nonworking_start_counts_from_next_open() -> None:
    cal = WorkingCalendar()
    # Saturday noon + 1 business hour -> Monday 10:00 (no charge for the weekend).
    assert cal.advance(_dt(_SAT, 12), 1) == _dt(_MON + 7, 10)


def test_advance_full_multi_day_span() -> None:
    cal = WorkingCalendar()  # 8h/day
    # 16 business hours == two full days; exactly filling Tuesday lands on its
    # 17:00 boundary (consistent with the single-day boundary case).
    assert cal.advance(_dt(_MON, 9), 16) == _dt(_MON + 1, 17)
    # One business hour past the two full days spills into Wednesday morning.
    assert cal.advance(_dt(_MON, 9), 17) == _dt(_MON + 2, 10)


def test_advance_lands_on_day_boundary() -> None:
    cal = WorkingCalendar()
    # Exactly fills Monday -> the 17:00 boundary instant.
    assert cal.advance(_dt(_MON, 9), 8) == _dt(_MON, 17)


def test_advance_negative_rejected() -> None:
    cal = WorkingCalendar()
    with pytest.raises(ValueError):
        cal.advance(_dt(_MON, 9), -1)


def test_advance_preserves_tzinfo() -> None:
    cal = WorkingCalendar()
    result = cal.advance(_dt(_MON, 16, tz=True), 2)
    assert result == _dt(_MON + 1, 10, tz=True)
    assert result.tzinfo is UTC


def test_next_free_slot_empty_calendar() -> None:
    cal = WorkingCalendar()
    slot = cal.next_free_slot(_dt(_MON, 9), timedelta(hours=1))
    assert slot == _dt(_MON, 9)


def test_next_free_slot_snaps_into_working_hours() -> None:
    cal = WorkingCalendar()
    slot = cal.next_free_slot(_dt(_MON, 7), timedelta(hours=1))
    assert slot == _dt(_MON, 9)


def test_next_free_slot_avoids_busy_interval() -> None:
    cal = WorkingCalendar()
    busy = [(_dt(_MON, 9), _dt(_MON, 10))]
    slot = cal.next_free_slot(_dt(_MON, 9), timedelta(hours=1), busy)
    assert slot == _dt(_MON, 10)


def test_next_free_slot_fits_between_two_meetings() -> None:
    cal = WorkingCalendar()
    busy = [(_dt(_MON, 9), _dt(_MON, 10)), (_dt(_MON, 11), _dt(_MON, 12))]
    # 10:00-11:00 is a free hour between them.
    slot = cal.next_free_slot(_dt(_MON, 9), timedelta(hours=1), busy)
    assert slot == _dt(_MON, 10)


def test_next_free_slot_busy_unordered_input() -> None:
    cal = WorkingCalendar()
    busy = [(_dt(_MON, 11), _dt(_MON, 12)), (_dt(_MON, 9), _dt(_MON, 10))]
    slot = cal.next_free_slot(_dt(_MON, 9), timedelta(hours=2), busy)
    # Needs 2 contiguous hours; first fit is 12:00-14:00.
    assert slot == _dt(_MON, 12)


def test_next_free_slot_rolls_to_next_day_when_no_room() -> None:
    cal = WorkingCalendar()
    # 16:00 start, 2h slot would run past 17:00 -> next day 09:00.
    slot = cal.next_free_slot(_dt(_MON, 16), timedelta(hours=2))
    assert slot == _dt(_MON + 1, 9)


def test_next_free_slot_touching_endpoint_is_not_a_conflict() -> None:
    cal = WorkingCalendar()
    busy = [(_dt(_MON, 10), _dt(_MON, 11))]
    # A 09:00-10:00 slot ends exactly when the meeting starts: no overlap.
    slot = cal.next_free_slot(_dt(_MON, 9), timedelta(hours=1), busy)
    assert slot == _dt(_MON, 9)


def test_next_free_slot_duration_too_long_rejected() -> None:
    cal = WorkingCalendar()
    with pytest.raises(ValueError):
        cal.next_free_slot(_dt(_MON, 9), timedelta(hours=9))


def test_next_free_slot_nonpositive_duration_rejected() -> None:
    cal = WorkingCalendar()
    with pytest.raises(ValueError):
        cal.next_free_slot(_dt(_MON, 9), timedelta(0))


def test_custom_window_and_weekdays() -> None:
    cal = WorkingCalendar(
        day_start=time(8, 0),
        day_end=time(16, 0),
        working_weekdays=frozenset({0, 1, 2, 3, 4, 5}),  # include Saturday
    )
    assert cal.is_working(_dt(_SAT, 9))
    assert not cal.is_working(_dt(_SUN, 9))
    assert cal.hours_per_day == 8.0
    assert cal.is_working(_dt(_MON, 8))
    assert not cal.is_working(_dt(_MON, 16))


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError):
        WorkingCalendar(day_start=time(17, 0), day_end=time(9, 0))


def test_empty_weekdays_rejected() -> None:
    with pytest.raises(ValueError):
        WorkingCalendar(working_weekdays=frozenset())
