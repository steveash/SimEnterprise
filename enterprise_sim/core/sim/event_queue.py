"""The discrete-event priority queue and its stable ordering key (§15.1, D26).

The engine is event-driven, not fixed-tick (ARCHITECTURE.md §15.1): future work
lives in a min-heap of scheduled items keyed by *when* they fire. Popping the
earliest, executing it, and enqueueing whatever it spawns is the whole loop.

Determinism (D26) hinges on the **order** two items pop in never depending on the
race to insert them. So the heap is ordered by an explicit, total
:class:`ScheduleKey` — ``(timestamp, process_priority, instance_id, step_id)`` —
not by insertion sequence. Two runs from the same seed enqueue the same items
with the same keys and therefore pop them in the same order, regardless of how
the surrounding code interleaves. The keys are designed to be unique per
scheduled item; an insertion counter is retained purely as a final tie-break so
that an accidental exact-key collision still yields a defined (FIFO) order rather
than relying on heap-implementation details.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True, order=True)
class ScheduleKey:
    """The stable total order over scheduled items (D26).

    Comparison is field-by-field in declaration order, so items sort by firing
    ``timestamp`` first, then by ``process_priority`` (lower fires first — a
    higher-priority process), then lexicographically by ``instance_id`` and
    ``step_id`` to make the order total and reproducible. Frozen and orderable so
    it can key a heap directly.
    """

    timestamp: datetime
    process_priority: int
    instance_id: str
    step_id: str


@dataclass(slots=True, order=True)
class _Entry[T]:
    """A heap node: ordered by ``key`` then insertion ``seq`` only.

    ``item`` is excluded from comparison (``compare=False``) so the payload is
    never compared — it need not be orderable, and equal keys fall back to the
    monotonic ``seq`` for a defined FIFO tie-break.
    """

    key: ScheduleKey
    seq: int
    item: T = field(compare=False)


class EventQueue[T]:
    """A min-heap of payloads keyed by :class:`ScheduleKey`.

    Each payload (a scheduled activation/step, opaque to the queue) is pushed with
    a key and popped in stable key order. Generic over the payload type so callers
    keep their own static typing through the queue.
    """

    def __init__(self) -> None:
        self._heap: list[_Entry[T]] = []
        self._counter: Iterator[int] = itertools.count()

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def push(self, key: ScheduleKey, item: T) -> None:
        """Enqueue ``item`` to fire at ``key``."""
        heapq.heappush(self._heap, _Entry(key, next(self._counter), item))

    def pop(self) -> tuple[ScheduleKey, T]:
        """Remove and return the earliest ``(key, item)``.

        Raises:
            IndexError: If the queue is empty.
        """
        if not self._heap:
            raise IndexError("pop from an empty EventQueue")
        entry = heapq.heappop(self._heap)
        return (entry.key, entry.item)

    def peek(self) -> tuple[ScheduleKey, T]:
        """Return the earliest ``(key, item)`` without removing it.

        Raises:
            IndexError: If the queue is empty.
        """
        if not self._heap:
            raise IndexError("peek into an empty EventQueue")
        entry = self._heap[0]
        return (entry.key, entry.item)

    def drain(self) -> Iterator[tuple[ScheduleKey, T]]:
        """Yield every ``(key, item)`` in stable order, emptying the queue.

        Re-entrant-safe for the producer/consumer loop: items pushed during
        iteration are honoured (they will be yielded if their key has not yet
        passed), which is exactly the cascade behaviour the scheduler relies on.
        """
        while self._heap:
            yield self.pop()
