"""The consistency validator — Layer D's soft cross-check pass (ARCHITECTURE §11.4, D17).

After a run is fully assembled, three families of cross-checks sweep the built
knowledge graph and journal. Each is strictly **report-and-continue** (D17): a
finding becomes a row in ``validation/issues.jsonl`` and is summarised in the
manifest, but never raises — a real run that tripped a check still completes and
keeps every artifact it produced.

The checks:

* **Dangling references** — an edge endpoint (``src``/``dst``), an event subject,
  an event actor, or an event's ``parent_event`` that names a node/event the run
  does not contain. The store deliberately does *not* enforce endpoint existence
  (``World.add_edge``), leaving this detection here.
* **Scheduling conflicts** — two ``CalendarEvent`` bookings for the *same* person
  whose ``[start, end)`` intervals overlap. The scheduler books non-overlapping
  slots and only forces an overlap as a last resort (``forced_overlap``); this is
  the independent, KG-level confirmation.
* **Out-of-window stamps** — a node, edge, or event whose sim-time falls outside
  the configured simulation window ``[period_start, period_end]`` (whole-day
  inclusive).

The validator is a pure function of its inputs (no clock, no filesystem), so a
seed reproduces the same issue rows — and thus the same manifest summary.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from enterprise_sim.core.events import Event
from enterprise_sim.core.world import World
from enterprise_sim.producers.artifact import ValidationIssue

__all__ = [
    "DANGLING_EDGE_ENDPOINT",
    "DANGLING_EVENT_REFERENCE",
    "OUT_OF_WINDOW",
    "SCHEDULING_CONFLICT",
    "summarize_issue_rows",
    "validate_consistency",
]

# Stable machine tags for the ``kind`` column of ``validation/issues.jsonl``.
DANGLING_EDGE_ENDPOINT = "dangling_edge_endpoint"
DANGLING_EVENT_REFERENCE = "dangling_event_reference"
SCHEDULING_CONFLICT = "scheduling_conflict"
OUT_OF_WINDOW = "out_of_window"

# The node type the scheduler materializes per booking (§15.4) — the unit a
# scheduling-conflict check reasons over.
_CALENDAR_EVENT = "CalendarEvent"


def validate_consistency(
    world: World,
    events: Iterable[Event],
    *,
    window: tuple[datetime, datetime],
) -> list[ValidationIssue]:
    """Return every consistency finding for a built run, in a deterministic order.

    Runs the dangling-reference, scheduling-conflict, and out-of-window checks
    over ``world`` and ``events`` and returns their findings as soft
    :class:`~enterprise_sim.producers.artifact.ValidationIssue` rows. Never
    raises on a finding (D17): the worst a check does is append an issue.

    Args:
        world: The fully-built knowledge graph (Layer A structure + Layer B/C
            nodes and edges).
        events: The run's temporal journal (the combined event log). Kept
            separate because events live in the journal, not the graph store.
        window: The inclusive ``(start, end)`` simulation window. A sim-time is
            out-of-window when it sorts before ``start`` or after ``end``.

    Returns:
        The issues, grouped by check (dangling endpoints, dangling event
        references, scheduling conflicts, out-of-window stamps) and sorted within
        each group for stable, line-diffable output.
    """
    events = list(events)
    issues: list[ValidationIssue] = []
    issues.extend(_dangling_edge_endpoints(world))
    issues.extend(_dangling_event_references(world, events))
    issues.extend(_scheduling_conflicts(world))
    issues.extend(_out_of_window(world, events, window))
    return issues


def summarize_issue_rows(rows: Iterable[dict[str, object]]) -> dict[str, object]:
    """Summarize ``issues.jsonl`` rows into the manifest's ``validation`` block.

    Counts every row (from any source — scheduler, producers, and this
    validator) and tallies it by ``kind`` so the manifest is a self-describing
    index of how clean the run is. ``by_kind`` is sorted for a stable manifest.
    """
    rows = list(rows)
    by_kind: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("kind", ""))
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "total": len(rows),
        "by_kind": {kind: by_kind[kind] for kind in sorted(by_kind)},
    }


# --------------------------------------------------------------------------- #
# Checks.
# --------------------------------------------------------------------------- #


def _dangling_edge_endpoints(world: World) -> list[ValidationIssue]:
    """Flag every edge whose ``src`` or ``dst`` is not a node in the graph."""
    issues: list[ValidationIssue] = []
    for edge in sorted(world.edges(), key=lambda e: e.id):
        for role, node_id in (("src", edge.src), ("dst", edge.dst)):
            if world.get_node(node_id) is None:
                issues.append(
                    ValidationIssue(
                        kind=DANGLING_EDGE_ENDPOINT,
                        message=(
                            f"edge {edge.id!r} ({edge.type}) {role} {node_id!r} "
                            f"is not a node in the graph"
                        ),
                        where=edge.id,
                        details={"edge_type": edge.type, "endpoint": role, "missing": node_id},
                    )
                )
    return issues


def _dangling_event_references(
    world: World, events: Iterable[Event]
) -> list[ValidationIssue]:
    """Flag event subjects/actors that name no node, and unknown ``parent_event`` ids."""
    issues: list[ValidationIssue] = []
    known_events = {event.id for event in events}
    for event in sorted(events, key=lambda e: e.id):
        for subject in event.subjects:
            if world.get_node(subject) is None:
                issues.append(
                    _event_ref_issue(event.id, "subject", subject)
                )
        for role, person_ids in sorted(event.actors.items()):
            for person_id in person_ids:
                if world.get_node(person_id) is None:
                    issues.append(
                        _event_ref_issue(event.id, f"actor:{role}", person_id)
                    )
        if event.parent_event is not None and event.parent_event not in known_events:
            issues.append(
                _event_ref_issue(event.id, "parent_event", event.parent_event)
            )
    return issues


def _event_ref_issue(event_id: str, slot: str, missing: str) -> ValidationIssue:
    """Build one dangling-event-reference issue row."""
    return ValidationIssue(
        kind=DANGLING_EVENT_REFERENCE,
        message=f"event {event_id!r} {slot} {missing!r} is not present in the run",
        where=event_id,
        details={"slot": slot, "missing": missing},
    )


def _scheduling_conflicts(world: World) -> list[ValidationIssue]:
    """Flag overlapping ``CalendarEvent`` bookings for the same person.

    Bookings are grouped per person and scanned in start order; a booking that
    begins before the running end of an earlier (overlapping) booking is a
    conflict reported against the later booking, naming the booking it collides
    with.
    """
    by_person: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for node in world.nodes_by_type(_CALENDAR_EVENT):
        person = node.props.get("person")
        start = _parse_stamp(node.props.get("start"))
        end = _parse_stamp(node.props.get("end"))
        if not isinstance(person, str) or start is None or end is None:
            continue
        by_person.setdefault(person, []).append((start, end, node.id))

    issues: list[ValidationIssue] = []
    for person in sorted(by_person):
        bookings = sorted(by_person[person])
        prev_end, prev_id = bookings[0][1], bookings[0][2]
        for start, end, node_id in bookings[1:]:
            if start < prev_end:
                issues.append(
                    ValidationIssue(
                        kind=SCHEDULING_CONFLICT,
                        message=(
                            f"calendar event {node_id!r} for {person!r} overlaps "
                            f"{prev_id!r}"
                        ),
                        where=node_id,
                        details={
                            "person": person,
                            "conflicts_with": prev_id,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                        },
                    )
                )
            # Extend the running interval so a chain of overlaps is all caught.
            if end > prev_end:
                prev_end, prev_id = end, node_id
    return issues


def _out_of_window(
    world: World,
    events: Iterable[Event],
    window: tuple[datetime, datetime],
) -> list[ValidationIssue]:
    """Flag any node, edge, or event stamped outside the simulation window."""
    start, end = window
    issues: list[ValidationIssue] = []

    def out(stamp: datetime) -> bool:
        return stamp < start or stamp > end

    for node in sorted(world.nodes(), key=lambda n: n.id):
        if out(node.created_at):
            issues.append(_window_issue("node", node.id, node.created_at, window))
    for edge in sorted(world.edges(), key=lambda e: e.id):
        if out(edge.created_at):
            issues.append(_window_issue("edge", edge.id, edge.created_at, window))
    for event in sorted(events, key=lambda e: e.id):
        if out(event.timestamp):
            issues.append(_window_issue("event", event.id, event.timestamp, window))
    return issues


def _window_issue(
    element: str, element_id: str, stamp: datetime, window: tuple[datetime, datetime]
) -> ValidationIssue:
    """Build one out-of-window issue row."""
    start, end = window
    return ValidationIssue(
        kind=OUT_OF_WINDOW,
        message=(
            f"{element} {element_id!r} stamped {stamp.isoformat()} is outside the "
            f"window [{start.isoformat()}, {end.isoformat()}]"
        ),
        where=element_id,
        details={
            "element": element,
            "stamp": stamp.isoformat(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        },
    )


def _parse_stamp(value: object) -> datetime | None:
    """Parse an ISO-8601 ``CalendarEvent`` stamp, or ``None`` if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
