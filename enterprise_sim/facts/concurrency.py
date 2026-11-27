"""Concurrent days (``[concurrency] days``): two teams in the same hours, a fact found mid-day.

The strongest gym sets are concurrent: two teams work in parallel and one finds a fact
the other needs while both are at work. A world has the ingredients (a project that
depends on another team's project cites that project's ``status`` and ``target_date``),
but every document is at its day's start and a change is never found mid-day. On each
of ``days`` concurrent days :func:`plan_concurrency`:

* picks a cross-team dependency, project B citing a fact of project A whose later
  version changes inside the window, on a day of its own;
* moves that change to a mid-day time ``T`` (11:00 to 14:45) of its day;
* plants three ``work_log`` events: A's discoverer at ``T`` (the first A document
  after the change, so it announces it), and B's two workers at ``T - 2h`` (the old
  value is still true) and ``T + 2h`` (B has not heard: it states the old value);
* records the day in the gold graph as a ``ConcurrentDay`` node, with the event ids,
  the people and the window, which the knowledge layer exports as ``workstreams``.

A pure function of ``(world, ledger, config, seed)``. With ``days`` 0 nothing changes.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any

from enterprise_sim.core.events.event import Deliverable, Event
from enterprise_sim.core.world import Node, World
from enterprise_sim.facts.ledger import Ledger

__all__ = ["KIND", "N_CONCURRENT_DAY", "plan_concurrency"]

N_CONCURRENT_DAY = "ConcurrentDay"
KIND = "work_log"
_CITED = ("target_date", "status")
_GAP = timedelta(hours=2)


def _staff(world: World, project: str) -> list[str]:
    """The project's people in a stable order (``member_of`` edges)."""
    return sorted(e.src for e in world.in_edges(project, "member_of"))


def _initiative(world: World, project: str) -> str | None:
    out = [e.dst for e in world.out_edges(project, "under")]
    return out[0] if out else None


def plan_concurrency(
    world: World,
    ledger: Ledger,
    days: int,
    *,
    seed: int,
    start: datetime,
    end: datetime,
    attributes: tuple[str, ...] = _CITED,
) -> list[Event]:
    """Plant ``days`` concurrent days; return the planted events (to add to the journal)."""
    if days <= 0 or not ledger:
        return []
    rng = random.Random(f"{seed}\x00concurrency")
    candidates: list[tuple[str, str, str, int]] = []
    for needer, deps in sorted(ledger.depends_on.items()):
        for owner in deps:
            for fact_id in ledger.by_project.get(owner, []):
                record = ledger.facts[fact_id]
                if record.attribute.name not in attributes:
                    continue
                for v in record.versions[1:]:
                    moment = v.valid_from
                    if start + _GAP < moment and moment + timedelta(days=1) < end:
                        candidates.append((needer, owner, fact_id, v.number))
    rng.shuffle(candidates)
    planted: list[Event] = []
    used_days: set[Any] = set()
    used_versions: set[tuple[str, int]] = set()
    for needer, owner, fact_id, number in candidates:
        if len(used_days) >= days:
            break
        record = ledger.facts[fact_id]
        version = record.versions[number - 1]
        day = version.valid_from.date()
        workers = _staff(world, needer)
        owners = _staff(world, owner)
        if day in used_days or (fact_id, number) in used_versions or len(workers) < 2 or not owners:
            continue
        found = datetime.combine(day, time(rng.randint(11, 14), rng.choice((0, 15, 30, 45))))
        # the change now happens at `found`; the version before it ends there
        record.versions[number - 1] = replace(version, valid_from=found)
        previous = record.versions[number - 2]
        record.versions[number - 2] = replace(previous, valid_to=found)
        discoverer = rng.choice(owners)
        pair = rng.sample(workers, 2)
        key = f"{day.isoformat()}:{fact_id}"
        events = {
            "discovery": Event(
                id=f"evt:concurrency:{key}:discovery",
                type="ConcurrentFinding",
                timestamp=found,
                actors={"author": [discoverer]},
                initiative=_initiative(world, owner),
                project=owner,
                deliverable=Deliverable(kind=KIND, medium="document"),
                payload={
                    "topic": f"what the team found today about {record.subject_name}",
                    "required_facts": [fact_id],
                },
            ),
            "before": Event(
                id=f"evt:concurrency:{key}:before",
                type="ConcurrentWork",
                timestamp=found - _GAP,
                actors={"author": [pair[0]]},
                initiative=_initiative(world, needer),
                project=needer,
                deliverable=Deliverable(kind=KIND, medium="document"),
                payload={
                    "topic": f"this morning's work that depends on {record.subject_name}",
                    "required_facts": [fact_id],
                    "forced_stances": {fact_id: "current"},
                },
            ),
            "after": Event(
                id=f"evt:concurrency:{key}:after",
                type="ConcurrentWork",
                timestamp=found + _GAP,
                actors={"author": [pair[1]]},
                initiative=_initiative(world, needer),
                project=needer,
                deliverable=Deliverable(kind=KIND, medium="document"),
                payload={
                    "topic": f"this afternoon's work that depends on {record.subject_name}",
                    "required_facts": [fact_id],
                    "forced_stances": {fact_id: "stale"},
                },
            ),
        }
        planted.extend(events.values())
        node_id = f"concurrent:{key}"
        world.add_node(
            Node(
                node_id,
                N_CONCURRENT_DAY,
                found,
                props={
                    "day": day.isoformat(),
                    "fact": fact_id,
                    "version": number,
                    "discovered_at": found.isoformat(),
                    "discoverer": discoverer,
                    "owner_project": owner,
                    "needer_project": needer,
                    "needer_people": pair,
                    "events": {k: e.id for k, e in events.items()},
                    "window_start": (found - _GAP).isoformat(),
                    "window_end": (found + _GAP).isoformat(),
                },
            )
        )
        used_days.add(day)
        used_versions.add((fact_id, number))
    return planted
