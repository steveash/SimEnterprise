"""The append-only event journal (ARCHITECTURE.md §11.2/§11.4).

The KG is the materialized *current* state; the journal is the **append-only**
record of *what happened when*. It is a first-class output (``kg/events.jsonl``)
and the temporal ground truth for replay, debugging, and eval. Two guarantees:

* **Append-only** — events are never mutated or removed in place; ``append``
  rejects duplicate ids so the journal stays a faithful log.
* **Stable ordering** — :meth:`ordered` sorts deterministically by
  ``(timestamp, id)`` so the same seed yields byte-identical JSONL that two runs
  ``git``-diff cleanly. Insertion order is preserved separately for replay.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import TextIO

from enterprise_sim.core.events.event import Event


class EventJournal:
    """An append-only, ordered log of :class:`Event` objects."""

    def __init__(self, events: Iterable[Event] | None = None) -> None:
        self._events: list[Event] = []
        self._ids: set[str] = set()
        if events is not None:
            self.extend(events)

    def append(self, event: Event) -> None:
        """Append one event. Raises ``ValueError`` on a duplicate id.

        Duplicate-id rejection keeps the journal append-only and unambiguous:
        ids are content-derived and deterministic (§11.1), so a collision means
        a genuine bug, not a legitimate re-emission.
        """
        if event.id in self._ids:
            raise ValueError(f"duplicate event id in journal: {event.id!r}")
        self._ids.add(event.id)
        self._events.append(event)

    def extend(self, events: Iterable[Event]) -> None:
        """Append events in iteration order."""
        for event in events:
            self.append(event)

    def __iter__(self) -> Iterator[Event]:
        """Iterate events in insertion order."""
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._ids

    def ordered(self) -> list[Event]:
        """Return events in the canonical, deterministic order.

        Sorted by ``(timestamp, id)``. Python's sort is stable, so events
        sharing a timestamp fall back to id order — total and reproducible.
        """
        return sorted(self._events, key=lambda e: (e.timestamp, e.id))

    def to_jsonl(self, stream: TextIO) -> None:
        """Write the journal to *stream* as canonical JSONL (one event per line).

        Rows are emitted in :meth:`ordered` order with sorted keys so two runs
        with the same seed produce identical, line-diffable files.
        """
        for event in self.ordered():
            stream.write(json.dumps(event.to_dict(), sort_keys=True))
            stream.write("\n")

    def dumps(self) -> str:
        """Return the canonical JSONL serialization as a string."""
        return "".join(
            json.dumps(event.to_dict(), sort_keys=True) + "\n" for event in self.ordered()
        )

    @classmethod
    def from_jsonl(cls, stream: Iterable[str]) -> EventJournal:
        """Load a journal from JSONL lines (blank lines are skipped)."""
        journal = cls()
        for line in stream:
            line = line.strip()
            if not line:
                continue
            journal.append(Event.from_dict(json.loads(line)))
        return journal
