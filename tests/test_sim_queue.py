"""Tests for the discrete-event queue, ScheduleKey, and Clock (esim-f8a5960b).

Acceptance: deterministic pop order under the stable key
``(timestamp, process_priority, instance_id, step_id)`` independent of insertion
order; monotonic clock.
"""

from __future__ import annotations

import random
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from enterprise_sim.core.sim import Clock, EventQueue, ScheduleKey


def _key(
    *,
    ts: datetime,
    priority: int = 0,
    instance: str = "i0",
    step: str = "s0",
) -> ScheduleKey:
    return ScheduleKey(timestamp=ts, process_priority=priority, instance_id=instance, step_id=step)


def _ts(minute: int) -> datetime:
    return datetime(2026, 6, 22, 9, minute, tzinfo=UTC)


def test_pop_orders_by_timestamp() -> None:
    q: EventQueue[str] = EventQueue()
    q.push(_key(ts=_ts(30)), "later")
    q.push(_key(ts=_ts(10)), "earlier")
    assert q.pop() == (_key(ts=_ts(10)), "earlier")
    assert q.pop() == (_key(ts=_ts(30)), "later")


def test_tiebreak_priority_then_instance_then_step() -> None:
    q: EventQueue[str] = EventQueue()
    ts = _ts(0)
    # Same timestamp; differ only by the later key fields.
    q.push(_key(ts=ts, priority=1, instance="b", step="z"), "low-prio")
    q.push(_key(ts=ts, priority=0, instance="b", step="z"), "high-prio")
    q.push(_key(ts=ts, priority=0, instance="a", step="z"), "instance-a")
    q.push(_key(ts=ts, priority=0, instance="a", step="a"), "step-a")
    order = [item for _, item in q.drain()]
    assert order == ["step-a", "instance-a", "high-prio", "low-prio"]


def test_pop_order_independent_of_insertion_order() -> None:
    keys = [
        _key(ts=_ts(m), priority=p, instance=f"i{i}", step=f"s{s}")
        for m in (5, 0, 9)
        for p in (1, 0)
        for i in (1, 0)
        for s in (1, 0)
    ]
    expected = sorted(keys)

    def drained(seed: int) -> list[ScheduleKey]:
        shuffled = list(keys)
        random.Random(seed).shuffle(shuffled)
        q: EventQueue[int] = EventQueue()
        for idx, k in enumerate(shuffled):
            q.push(k, idx)
        return [k for k, _ in q.drain()]

    # Every insertion permutation yields the same stable key order.
    assert drained(1) == expected
    assert drained(2) == expected
    assert drained(12345) == expected


def test_equal_keys_fall_back_to_fifo() -> None:
    q: EventQueue[str] = EventQueue()
    k = _key(ts=_ts(0))
    q.push(k, "first")
    q.push(k, "second")
    q.push(k, "third")
    assert [item for _, item in q.drain()] == ["first", "second", "third"]


def test_peek_does_not_remove() -> None:
    q: EventQueue[str] = EventQueue()
    q.push(_key(ts=_ts(10)), "a")
    assert q.peek() == (_key(ts=_ts(10)), "a")
    assert len(q) == 1
    assert q.peek() == q.pop()
    assert len(q) == 0


def test_len_and_bool() -> None:
    q: EventQueue[str] = EventQueue()
    assert not q
    assert len(q) == 0
    q.push(_key(ts=_ts(0)), "a")
    assert q
    assert len(q) == 1


def test_pop_empty_raises() -> None:
    q: EventQueue[str] = EventQueue()
    with pytest.raises(IndexError):
        q.pop()


def test_peek_empty_raises() -> None:
    q: EventQueue[str] = EventQueue()
    with pytest.raises(IndexError):
        q.peek()


def test_drain_honours_items_pushed_mid_iteration() -> None:
    q: EventQueue[str] = EventQueue()
    q.push(_key(ts=_ts(10)), "a")
    seen = []
    for _, item in q.drain():
        seen.append(item)
        if item == "a":
            # A cascade: executing "a" enqueues a later "b".
            q.push(_key(ts=_ts(20)), "b")
    assert seen == ["a", "b"]


def test_schedulekey_is_frozen() -> None:
    k = _key(ts=_ts(0))
    with pytest.raises(FrozenInstanceError):
        k.timestamp = _ts(1)  # type: ignore[misc]


# --- Clock ---------------------------------------------------------------


def test_clock_starts_at_given_time() -> None:
    start = _ts(0)
    assert Clock(start).now == start


def test_clock_advances_forward() -> None:
    clock = Clock(_ts(0))
    clock.advance_to(_ts(30))
    assert clock.now == _ts(30)


def test_clock_allows_same_instant() -> None:
    clock = Clock(_ts(10))
    clock.advance_to(_ts(10))
    assert clock.now == _ts(10)


def test_clock_rejects_backward() -> None:
    clock = Clock(_ts(30))
    with pytest.raises(ValueError):
        clock.advance_to(_ts(10))


def test_clock_driven_by_queue_is_monotonic() -> None:
    q: EventQueue[str] = EventQueue()
    for minute in (40, 0, 20, 10):
        q.push(_key(ts=_ts(minute)), f"e{minute}")
    clock = Clock(_ts(0))
    times = []
    for key, _ in q.drain():
        clock.advance_to(key.timestamp)
        times.append(clock.now)
    assert times == [_ts(0), _ts(10), _ts(20), _ts(40)]
    assert times == sorted(times)


def test_clock_with_timedelta_window() -> None:
    start = datetime(2026, 6, 22, 9, tzinfo=UTC)
    clock = Clock(start)
    clock.advance_to(start + timedelta(hours=8))
    assert clock.now == datetime(2026, 6, 22, 17, tzinfo=UTC)
