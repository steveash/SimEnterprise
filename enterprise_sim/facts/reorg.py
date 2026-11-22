"""The ``reporting_line`` fact of a reorganized project (``[org] reorgs``).

Not drawn at random like the catalog's attributes: its two versions come from the
reorg planned in the graph (:mod:`enterprise_sim.world_builders.reorgs`), so the
ledger, the documents' statements and the gold agree on the day the line changed.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time

from enterprise_sim.core.world import World
from enterprise_sim.facts.catalog import Attribute, Context

__all__ = ["REPORTING_LINE", "add_reorg_facts"]


def _fixed(rng: random.Random, ctx: Context) -> str:
    raise RuntimeError("reporting_line is never drawn: it comes from the reorg plan")


def _keep(rng: random.Random, old: str, ctx: Context) -> str:
    return old


REPORTING_LINE = Attribute(
    name="reporting_line",
    label="reporting line",
    draw=_fixed,
    mutate=_keep,
    current=(
        "{subject} reports into {value}.",
        "The reporting line for {subject} is {value}.",
    ),
    update="After the reorg, {subject} moved from {old} to {value}.",
    question="Which line does {subject} report into?",
    volatile=0.0,
)


def add_reorg_facts(world: World, ledger: object, *, start: date) -> None:
    """Add a two-version ``reporting_line`` fact for every reorganized project."""
    from enterprise_sim.facts.ledger import FactRecord, Ledger, Version, _slug
    from enterprise_sim.world_builders.reorgs import reporting_line

    assert isinstance(ledger, Ledger)
    t0 = datetime.combine(start, time(9, 0))
    for project in sorted(world.nodes_by_type("Project"), key=lambda n: n.id):
        at_text = project.props.get("reorg_at")
        if not at_text:
            continue
        at = datetime.fromisoformat(str(at_text))
        old = reporting_line(world, str(project.props["reorg_from"]))
        new = reporting_line(world, str(project.props["reorg_to"]))
        fact_id = f"fact:{_slug(project.id.split(':', 1)[-1])}:reporting_line"
        record = FactRecord(
            fact_id,
            project.id,
            str(project.props.get("name", project.id)),
            str(project.props.get("department", "")),
            REPORTING_LINE,
        )
        record.versions = [Version(1, old, t0, at), Version(2, new, at, None)]
        record.horizon_values = {old, new}
        ledger.facts[fact_id] = record
        ledger.by_project.setdefault(project.id, []).append(fact_id)
