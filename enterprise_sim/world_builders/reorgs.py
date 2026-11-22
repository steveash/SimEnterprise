"""Reorgs mid-window (``[org] reorgs``): a project moves to another department on a date.

:func:`plan_reorgs` picks ``count`` projects (never walled ones, decision record
0036) and, for each, another department and a day drawn over ``[org] horizon_days``
from the window start (fixed, like turnover, so extending the window never moves a
past reorg; one beyond the window is skipped). It writes the plan into the Layer-A
graph: project props ``reorg_at``, ``reorg_from``, ``reorg_to`` and a dated
``reorganized_into`` edge to the new department.

The fact ledger turns each reorg into a ``reporting_line`` fact with two versions
(:func:`enterprise_sim.facts.reorg.add_reorg_facts`): "<old department> under
<its head>" until the day, "<new department> under <its head>" from it. Documents
then state the line with the ledger's usual stances (current, update, stale), so a
memory system must notice that the line changed. The project's staff and the kind
of documents it writes do not change.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from enterprise_sim.core.world import Edge, World

__all__ = ["E_REORGANIZED_INTO", "plan_reorgs", "reporting_line"]

E_REORGANIZED_INTO = "reorganized_into"


def _departments(world: World) -> dict[str, str]:
    """Department archetype name -> department node id."""
    out = {}
    for node in world.nodes_by_type("Department"):
        name = str(node.props.get("archetype") or node.props.get("name") or "")
        out[name] = node.id
    return out


def reporting_line(world: World, department: str) -> str:
    """'<Department> under <head>' for a department archetype name."""
    display = department.replace("_", " ").title()
    dept_id = _departments(world).get(department)
    heads = [e.src for e in world.in_edges(dept_id, "leads")] if dept_id else []
    head = world.get_node(heads[0]) if heads else None
    name = head.props.get("name") if head is not None else None
    return f"{display} under {name}" if name else display


def plan_reorgs(
    world: World,
    count: int,
    rng: random.Random,
    *,
    start: datetime,
    span_days: int,
    horizon_days: int,
) -> list[str]:
    """Move ``count`` projects to another department on a dated day; return their ids."""
    if count <= 0:
        return []
    departments = sorted(_departments(world))
    if len(departments) < 2:
        return []
    projects = sorted(
        (
            n
            for n in world.nodes_by_type("Project")
            if not n.props.get("walled") and n.props.get("department") in departments
        ),
        key=lambda n: n.id,
    )
    rng.shuffle(projects)
    moved: list[str] = []
    for project in projects[:count]:
        own = str(project.props["department"])
        target = rng.choice([d for d in departments if d != own])
        day = rng.randint(14, max(14, horizon_days - 1))
        at = datetime.combine((start + timedelta(days=day)).date(), time(9, 0))
        if day > span_days - 7:
            continue  # after (or too close to) the window end: not in this world yet
        project.props["reorg_at"] = at.isoformat()
        project.props["reorg_from"] = own
        project.props["reorg_to"] = target
        dept_id = _departments(world)[target]
        world.add_edge(
            Edge(
                f"edge:{E_REORGANIZED_INTO}:{project.id}->{dept_id}",
                E_REORGANIZED_INTO,
                project.id,
                dept_id,
                at,
            )
        )
        moved.append(project.id)
    return moved
