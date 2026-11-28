"""What the planters (``facts.concurrency``, ``facts.caveats``) share: who can write a document.

A planted document is written by one of its project's people at a time; a person who
had not joined yet, or had already left, cannot be its author (the producer would drop
them, and the document would have no author). :func:`staff_at` is the one place both
planters pick from.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from enterprise_sim.core.world import World

__all__ = ["staff_at"]


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def staff_at(world: World, project: str, start: datetime, end: datetime) -> list[str]:
    """The project's people (``member_of``) employed over all of ``[start, end]``, sorted."""
    out: list[str] = []
    for edge in world.in_edges(project, "member_of"):
        node = world.get_node(edge.src)
        props = node.props if node is not None else {}
        joined, left = _iso(props.get("joined_at")), _iso(props.get("left_at"))
        if joined is not None and joined > start.isoformat():
            continue
        if left is not None and left <= end.isoformat():
            continue
        out.append(edge.src)
    return sorted(out)
