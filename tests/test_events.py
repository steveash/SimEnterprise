"""Tests for the Event contract and the append-only journal (esim-4ccfd15f).

Acceptance: append + stable ordering + serialize/deserialize.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from enterprise_sim.core.events import Deliverable, Event, EventJournal


def _event(
    event_id: str,
    *,
    ts: datetime,
    deliverable: Deliverable | None = None,
    parent: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        type="DeliverableDrafted",
        timestamp=ts,
        actors={"author": ["person:ada-lovelace"], "reviewers": ["person:alan-turing"]},
        initiative="init:payments",
        project="proj:payments-api",
        subjects=["art:weekly-status"],
        deliverable=deliverable,
        parent_event=parent,
        payload={"topic": "weekly status", "tone": "neutral"},
    )


def _ts(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 6, day, hour, 0, tzinfo=UTC)


# --- Deliverable round-trip ------------------------------------------------


def test_deliverable_round_trips() -> None:
    deliverable = Deliverable(kind="status_report", medium="document")
    assert Deliverable.from_dict(deliverable.to_dict()) == deliverable


def test_deliverable_is_frozen() -> None:
    deliverable = Deliverable(kind="status_report", medium="document")
    with pytest.raises(FrozenInstanceError):
        deliverable.kind = "design_doc"  # type: ignore[misc]


# --- Event serialize / deserialize -----------------------------------------


def test_event_to_dict_shape() -> None:
    event = _event("evt:1", ts=_ts(19), deliverable=Deliverable("status_report", "document"))
    data = event.to_dict()
    assert data["id"] == "evt:1"
    assert data["timestamp"] == "2026-06-19T09:00:00+00:00"
    assert data["deliverable"] == {"kind": "status_report", "medium": "document"}
    assert data["actors"] == {
        "author": ["person:ada-lovelace"],
        "reviewers": ["person:alan-turing"],
    }


def test_event_round_trips_with_deliverable() -> None:
    event = _event("evt:1", ts=_ts(19), deliverable=Deliverable("design_doc", "document"))
    assert Event.from_dict(event.to_dict()) == event


def test_event_round_trips_without_deliverable() -> None:
    event = _event("evt:2", ts=_ts(20), parent="evt:1")
    restored = Event.from_dict(event.to_dict())
    assert restored == event
    assert restored.deliverable is None
    assert restored.parent_event == "evt:1"


def test_event_from_dict_accepts_datetime_timestamp() -> None:
    # from_dict should tolerate an already-parsed datetime, not only a string.
    event = _event("evt:3", ts=_ts(21))
    data = event.to_dict()
    data["timestamp"] = event.timestamp
    assert Event.from_dict(data) == event


def test_to_dict_does_not_alias_mutable_fields() -> None:
    event = _event("evt:4", ts=_ts(22))
    data = event.to_dict()
    data["subjects"].append("art:leaked")
    data["actors"]["author"].append("person:mallory")
    assert event.subjects == ["art:weekly-status"]
    assert event.actors["author"] == ["person:ada-lovelace"]


# --- Journal: append --------------------------------------------------------


def test_journal_append_preserves_insertion_order() -> None:
    journal = EventJournal()
    journal.append(_event("evt:b", ts=_ts(21)))
    journal.append(_event("evt:a", ts=_ts(20)))
    assert [e.id for e in journal] == ["evt:b", "evt:a"]
    assert len(journal) == 2


def test_journal_rejects_duplicate_ids() -> None:
    journal = EventJournal()
    journal.append(_event("evt:1", ts=_ts(19)))
    with pytest.raises(ValueError, match="duplicate event id"):
        journal.append(_event("evt:1", ts=_ts(20)))


def test_journal_contains_and_constructor_seed() -> None:
    journal = EventJournal([_event("evt:1", ts=_ts(19)), _event("evt:2", ts=_ts(20))])
    assert "evt:1" in journal
    assert "evt:missing" not in journal
    assert len(journal) == 2


# --- Journal: stable ordering ----------------------------------------------


def test_ordered_sorts_by_timestamp_then_id() -> None:
    journal = EventJournal()
    journal.append(_event("evt:late", ts=_ts(22)))
    journal.append(_event("evt:b", ts=_ts(20)))
    journal.append(_event("evt:a", ts=_ts(20)))  # same ts as evt:b -> id breaks the tie
    assert [e.id for e in journal.ordered()] == ["evt:a", "evt:b", "evt:late"]


def test_ordered_is_deterministic_regardless_of_append_order() -> None:
    events = [_event(f"evt:{i}", ts=_ts(19 + (i % 3))) for i in range(6)]
    forward = EventJournal(events)
    backward = EventJournal(list(reversed(events)))
    assert [e.id for e in forward.ordered()] == [e.id for e in backward.ordered()]


# --- Journal: JSONL serialize / deserialize --------------------------------


def test_journal_jsonl_round_trips() -> None:
    journal = EventJournal()
    journal.append(_event("evt:2", ts=_ts(20), deliverable=Deliverable("design_doc", "document")))
    journal.append(_event("evt:1", ts=_ts(19), parent="evt:0"))

    restored = EventJournal.from_jsonl(io.StringIO(journal.dumps()))

    # Round-trip preserves the canonical (ordered) sequence.
    assert [e.id for e in restored] == ["evt:1", "evt:2"]
    assert restored.ordered() == journal.ordered()


def test_dumps_emits_one_canonical_line_per_event() -> None:
    journal = EventJournal()
    journal.append(_event("evt:late", ts=_ts(22)))
    journal.append(_event("evt:early", ts=_ts(19)))

    lines = journal.dumps().splitlines()
    assert len(lines) == 2
    # Canonical order (early first) and sorted keys for clean line-diffs.
    assert lines[0].index('"id": "evt:early"') >= 0
    assert lines[0].index('"actors"') < lines[0].index('"type"')


def test_to_jsonl_writes_to_stream() -> None:
    journal = EventJournal([_event("evt:1", ts=_ts(19))])
    buffer = io.StringIO()
    journal.to_jsonl(buffer)
    assert buffer.getvalue() == journal.dumps()


def test_from_jsonl_skips_blank_lines() -> None:
    journal = EventJournal([_event("evt:1", ts=_ts(19))])
    text = "\n" + journal.dumps() + "\n\n"
    restored = EventJournal.from_jsonl(io.StringIO(text))
    assert [e.id for e in restored] == ["evt:1"]
