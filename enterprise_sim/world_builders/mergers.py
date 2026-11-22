"""A merger mid-window (``[org] merger_days``): an acquired company joins on a dated day.

:func:`plan_merger` names an acquired company, makes one department (``[org]
merger_department``, default the last one built) the department it brought, and draws
the merger day ``merger_days`` after the window start (none when that is beyond the
window). It writes the plan into the Layer-A graph:

* an ``Acquisition`` node for the acquired company (``name``, ``acquired_at``) and a
  dated ``acquired`` edge from the company. It is not a ``Company`` node: a world has
  exactly one company, and importers refuse a second;
* on the department: ``acquired_from`` and ``merged_at``.

The fact ledger gives each project of that department a ``system_of_record`` fact
with two versions (:func:`enterprise_sim.facts.merger.add_merger_facts`): the acquired
company's system until the merger day, the acquirer's from it. Two systems of one kind
coexist until the integration, the duplicate-entity situation a memory system must
resolve by date. Walled departments (decision record 0036) are never the acquired one.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from enterprise_sim.core.world import Edge, Node, World

__all__ = ["ACQUIRED_NAMES", "E_ACQUIRED", "plan_merger", "system_kind"]

E_ACQUIRED = "acquired"
ACQUIRED_NAMES = ("Northwind Systems", "Bluefin Labs", "Tamarack Group", "Larchfield Data")
_SYSTEM_KINDS = {
    "sales": "CRM",
    "support": "ticketing system",
    "engineering": "issue tracker",
    "finance": "general ledger",
    "marketing": "campaign platform",
    "people_ops": "HR system",
    "data_engineering": "data warehouse",
    "data_analytics": "BI platform",
}


def system_kind(department: str) -> str:
    return _SYSTEM_KINDS.get(department, "records system")


def plan_merger(
    world: World,
    rng: random.Random,
    *,
    start: datetime,
    span_days: int,
    merger_days: int,
    department: str | None,
) -> str | None:
    """Plan the merger; return the acquired department's archetype name (or ``None``)."""
    if merger_days <= 0 or merger_days > span_days - 7:
        return None
    departments = [n for n in world.nodes_by_type("Department")]
    walled = {
        str(p.props.get("department"))
        for p in world.nodes_by_type("Project")
        if p.props.get("walled")
    }
    candidates = [d for d in departments if str(d.props.get("archetype")) not in walled]
    if department is not None:
        candidates = [d for d in candidates if d.props.get("archetype") == department]
    if not candidates:
        return None
    dept = candidates[-1]
    at = datetime.combine((start + timedelta(days=merger_days)).date(), time(9, 0))
    name = rng.choice(ACQUIRED_NAMES)
    acquired_id = "acquisition:" + "-".join(name.lower().split())
    world.add_node(
        Node(
            acquired_id,
            "Acquisition",
            start,
            props={"name": name, "acquired_at": at.isoformat()},
            aliases=[name],
        )
    )
    acquirer = next((n.id for n in world.nodes_by_type("Company")), None)
    if acquirer is not None:
        world.add_edge(
            Edge(
                f"edge:{E_ACQUIRED}:{acquirer}->{acquired_id}",
                E_ACQUIRED,
                acquirer,
                acquired_id,
                at,
            )
        )
    dept.props["acquired_from"] = name
    dept.props["merged_at"] = at.isoformat()
    return str(dept.props.get("archetype"))
