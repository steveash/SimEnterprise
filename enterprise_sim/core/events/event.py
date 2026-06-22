"""The :class:`Event` contract and its abstract :class:`Deliverable`.

An ``Event`` is a *format-agnostic business event* bound to knowledge-graph
entities (ARCHITECTURE.md §5). The core simulates the enterprise and emits a
stream of these; every other layer only ever speaks ``Event`` + KG-entity, so
this type is the contract between Layer B (the simulator) and the producers /
KG. Events serialize to ``kg/events.jsonl`` — the append-only temporal journal
(§11.2/§11.4) — so (de)serialization is part of the contract, not an add-on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Deliverable:
    """An *abstract* deliverable request: a kind of output in some medium.

    Concrete files are produced later by an ``ArtifactProducer``; here we only
    name what is wanted, e.g. ``{"kind": "status_report", "medium": "document"}``.
    Frozen so an event's deliverable cannot be mutated after emission.
    """

    kind: str
    medium: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this deliverable."""
        return {"kind": self.kind, "medium": self.medium}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Deliverable:
        """Reconstruct a :class:`Deliverable` from :meth:`to_dict` output."""
        return cls(kind=data["kind"], medium=data["medium"])


@dataclass(slots=True)
class Event:
    """A business event emitted by a process and applied to the KG.

    Fields mirror ARCHITECTURE.md §5/§11.1. ``actors`` maps a role name to the
    person ids in that role (e.g. ``{"author": ["p1"], "reviewers": ["p2"]}``);
    ``subjects`` lists KG node ids the event is *about*; ``parent_event`` threads
    causal chains (a review comment points back at the draft event); ``payload``
    is the semantic brief handed to the producer (topic, intent, tone).
    """

    id: str
    type: str
    timestamp: datetime
    actors: dict[str, list[str]] = field(default_factory=dict)
    initiative: str | None = None
    project: str | None = None
    subjects: list[str] = field(default_factory=list)
    deliverable: Deliverable | None = None
    parent_event: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (a single ``events.jsonl`` row).

        ``timestamp`` is rendered as an ISO-8601 string; ``deliverable`` as its
        nested mapping or ``None``. Round-trips with :meth:`from_dict`.
        """
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "actors": {role: list(people) for role, people in self.actors.items()},
            "initiative": self.initiative,
            "project": self.project,
            "subjects": list(self.subjects),
            "deliverable": self.deliverable.to_dict() if self.deliverable is not None else None,
            "parent_event": self.parent_event,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        """Reconstruct an :class:`Event` from :meth:`to_dict` output."""
        deliverable = data.get("deliverable")
        actors_raw: Mapping[str, Sequence[str]] = data.get("actors") or {}
        return cls(
            id=data["id"],
            type=data["type"],
            timestamp=_parse_timestamp(data["timestamp"]),
            actors={role: list(people) for role, people in actors_raw.items()},
            initiative=data.get("initiative"),
            project=data.get("project"),
            subjects=list(data.get("subjects") or []),
            deliverable=Deliverable.from_dict(deliverable) if deliverable is not None else None,
            parent_event=data.get("parent_event"),
            payload=dict(data.get("payload") or {}),
        )


def _parse_timestamp(value: Any) -> datetime:
    """Parse a timestamp that is either a ``datetime`` or an ISO-8601 string."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
