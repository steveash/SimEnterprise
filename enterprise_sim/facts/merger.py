"""The ``system_of_record`` fact of a project in an acquired department (``[org] merger_days``).

Its two versions come from the merger planned in the graph
(:mod:`enterprise_sim.world_builders.mergers`): "<acquired company> <system>" until
the merger day, "<company> <system>" from it.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time

from enterprise_sim.core.world import World
from enterprise_sim.facts.catalog import Attribute, Context

__all__ = ["SYSTEM_OF_RECORD", "add_merger_facts"]


def _fixed(rng: random.Random, ctx: Context) -> str:
    raise RuntimeError("system_of_record is never drawn: it comes from the merger plan")


def _keep(rng: random.Random, old: str, ctx: Context) -> str:
    return old


SYSTEM_OF_RECORD = Attribute(
    name="system_of_record",
    label="system of record",
    draw=_fixed,
    mutate=_keep,
    current=(
        "{subject} keeps its records in {value}.",
        "The system of record for {subject} is {value}.",
    ),
    update="After the merger, {subject} moved its records from {old} to {value}.",
    question="Which system of record does {subject} use?",
    volatile=0.0,
)


def add_merger_facts(world: World, ledger: object, *, start: date) -> None:
    """Add a two-version ``system_of_record`` fact for each project of the acquired department."""
    from enterprise_sim.facts.ledger import FactRecord, Ledger, Version, _slug
    from enterprise_sim.world_builders.mergers import system_kind

    assert isinstance(ledger, Ledger)
    acquired = {
        str(d.props.get("archetype")): d
        for d in world.nodes_by_type("Department")
        if d.props.get("merged_at")
    }
    company = next(
        (
            str(n.props.get("name"))
            for n in world.nodes_by_type("Company")
            if not n.props.get("acquired_at")
        ),
        "the company",
    )
    t0 = datetime.combine(start, time(9, 0))
    for project in sorted(world.nodes_by_type("Project"), key=lambda n: n.id):
        dept = acquired.get(str(project.props.get("department")))
        if dept is None:
            continue
        at = datetime.fromisoformat(str(dept.props["merged_at"]))
        kind = system_kind(str(dept.props.get("archetype")))
        old = f"{dept.props['acquired_from']} {kind}"
        new = f"{company} {kind}"
        fact_id = f"fact:{_slug(project.id.split(':', 1)[-1])}:system_of_record"
        record = FactRecord(
            fact_id,
            project.id,
            str(project.props.get("name", project.id)),
            str(project.props.get("department", "")),
            SYSTEM_OF_RECORD,
        )
        record.versions = [Version(1, old, t0, at), Version(2, new, at, None)]
        record.horizon_values = {old, new}
        ledger.facts[fact_id] = record
        ledger.by_project.setdefault(project.id, []).append(fact_id)
