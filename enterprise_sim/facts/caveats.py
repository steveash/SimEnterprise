"""Caveats (``[caveats] count``): a decision's exception or adjustment that changes the value.

A plain fact transfers when a consumer restates it. A caveat transfers only when the
consumer also applies it: "the approved budget is $400k; $57k of it is held back until
the Q3 review, so $343k is spendable now". A consumer that knows only the base value
computes the wrong answer, and every other document states that base value without
the caveat. :func:`plan_caveats` plants ``count`` of them:

* it picks fact versions of the attributes in :data:`RULES` (several attributes per world,
  round-robin, one caveat per fact), each in force long enough to hold a caveat;
* it plants one ``decision_note`` document of the owner project, a few days into the
  version's validity, that states the value and the caveat sentence together;
* it records each caveat in the gold graph as a ``Caveat`` node: the base item, the
  kind (``adjustment``: the value to use is derived from the base; ``exception``: a
  scope gets its own value), the scope, the question it answers, the value before and
  after, the typed value after and, for an adjustment, the arithmetic that decides it.

A value after is never round (no multiple of 5 of its unit), so a numeric screen can
tell it from a drawn value, and it is never a value the fact's chain ever had.

A pure function of ``(world, ledger, config, seed)``. With ``count`` 0 nothing changes.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from enterprise_sim.core.events.event import Deliverable, Event
from enterprise_sim.core.world import Node, World
from enterprise_sim.facts.catalog import value_token
from enterprise_sim.facts.ledger import Ledger

__all__ = ["KIND", "N_CAVEAT", "RULES", "Planned", "plan_caveats"]

N_CAVEAT = "Caveat"
KIND = "decision_note"
_LEAD = timedelta(days=2)
_TAIL = timedelta(days=3)


@dataclass(frozen=True, slots=True)
class Planned:
    """One caveat's content, from the base value."""

    kind: str  # "adjustment" or "exception"
    scope: str
    question: str  # with {subject}
    sentence: str
    value_after: str
    typed: dict[str, Any]
    arithmetic: dict[str, Any] | None


def _money(value: int) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    return f"${value // 1000}k"


def _money_value(text: str) -> int:
    body = text.lstrip("$")
    if body.endswith("M"):
        return round(float(body[:-1]) * 1_000_000)
    return int(body.rstrip("k")) * 1000


def _budget(rng: random.Random, value: str) -> Planned | None:
    try:
        base = _money_value(value)
    except ValueError:
        return None
    after = round(base * (1 - rng.uniform(0.12, 0.28)))
    while True:  # to the shown precision ($1.23M, $343k), never a multiple of 5 of it
        step = 10_000 if after >= 1_000_000 else 1000
        after = after // step * step
        if (after // step) % 5:
            break
        after -= step
    if after <= 0:
        return None
    held = base - after
    return Planned(
        kind="adjustment",
        scope="spendable now",
        question="How much of the approved budget of {subject} is spendable now?",
        sentence=(
            f"Of that budget, {_money(held)} is held back until the Q3 review, "
            f"so {_money(after)} is spendable now."
        ),
        value_after=_money(after),
        typed={"type": "money", "unit": "USD", "amount": after},
        arithmetic={
            "op": "subtract",
            "base": base,
            "by": held,
            "result": after,
            "by_value": _money(held),
        },
    )


def _launch(rng: random.Random, value: str) -> Planned | None:
    try:
        base = date.fromisoformat(value)
    except ValueError:
        return None
    region = rng.choice(("EU", "APAC", "LATAM"))
    days = rng.choice((9, 11, 12, 13, 16, 17, 18, 19, 23, 26))
    after = base + timedelta(days=days)
    return Planned(
        kind="exception",
        scope=f"the {region} rollout",
        question=f"When does the {region} rollout of {{subject}} launch?",
        sentence=f"The {region} rollout follows on {after.isoformat()}.",
        value_after=after.isoformat(),
        typed={"type": "date", "value": after.isoformat()},
        arithmetic={"op": "add_days", "base": value, "by": days, "result": after.isoformat()},
    )


def _latency(rng: random.Random, value: str) -> Planned | None:
    head = value.split()[0]
    if not head.isdigit():
        return None
    after = int(head) * 2 + rng.choice((13, 17, 23, 27, 33, 37, 43, 47))
    return Planned(
        kind="exception",
        scope="batch endpoints",
        question="What is the p99 latency budget for the batch endpoints of {subject}?",
        sentence=f"Batch endpoints are exempt: their p99 budget is {after} ms.",
        value_after=f"{after} ms",
        typed={"type": "duration", "unit": "ms", "amount": after},
        arithmetic=None,
    )


def _margin(rng: random.Random, value: str) -> Planned | None:
    head = value.rstrip("%")
    if not head.isdigit():
        return None
    points = rng.choice((2, 3, 4, 6))
    after = int(head) - points
    while after % 5 == 0:
        points += 1
        after -= 1
    return Planned(
        kind="adjustment",
        scope="excluding the one-off migration credit",
        question=(
            "What is the gross margin target of {subject} excluding the one-off migration credit?"
        ),
        sentence=f"Excluding the one-off migration credit, the target is {after}%.",
        value_after=f"{after}%",
        typed={"type": "percent", "unit": "%", "amount": after},
        arithmetic={"op": "subtract_points", "base": int(head), "by": points, "result": after},
    )


#: attribute -> how a caveat changes its value (an attribute absent here takes none).
RULES: dict[str, Callable[[random.Random, str], Planned | None]] = {
    "budget": _budget,
    "target_date": _launch,
    "latency_target": _latency,
    "margin_target": _margin,
}


def _staff(world: World, project: str) -> list[str]:
    return sorted(e.src for e in world.in_edges(project, "member_of"))


def _initiative(world: World, project: str) -> str | None:
    out = [e.dst for e in world.out_edges(project, "under")]
    return out[0] if out else None


def plan_caveats(
    world: World,
    ledger: Ledger,
    count: int,
    *,
    seed: int,
    start: datetime,
    end: datetime,
    avoid: frozenset[str] = frozenset(),
) -> list[Event]:
    """Plant ``count`` caveats; return the planted events (to add to the journal).

    ``avoid``: facts another planter already uses (a concurrent day's), left alone.
    """
    if count <= 0 or not ledger:
        return []
    rng = random.Random(f"{seed}\x00caveats")
    by_attribute: dict[str, list[tuple[str, int]]] = {}
    for fact_id in sorted(ledger.facts):
        record = ledger.facts[fact_id]
        if record.attribute.name not in RULES or fact_id in avoid:
            continue
        for v in record.versions:
            first = max(v.valid_from, start) + _LEAD
            last = min(v.valid_to or end, end) - _TAIL
            if first < last:
                by_attribute.setdefault(record.attribute.name, []).append((fact_id, v.number))
    for pool in by_attribute.values():
        rng.shuffle(pool)
    order = sorted(by_attribute)
    planted: list[Event] = []
    used: set[str] = set()
    while len(used) < count and any(by_attribute.values()):
        for attribute in order:
            pool = by_attribute[attribute]
            while pool and len(used) < count:
                fact_id, number = pool.pop()
                if fact_id in used:
                    continue
                event = _plant(world, ledger, fact_id, number, rng, start=start, end=end)
                if event is not None:
                    planted.append(event)
                    used.add(fact_id)
                    break
    return planted


def _plant(
    world: World,
    ledger: Ledger,
    fact_id: str,
    number: int,
    rng: random.Random,
    *,
    start: datetime,
    end: datetime,
) -> Event | None:
    record = ledger.facts[fact_id]
    version = record.versions[number - 1]
    planned = RULES[record.attribute.name](rng, version.value)
    if planned is None or planned.value_after in {v.value for v in record.versions}:
        return None
    owner = record.subject
    staff = _staff(world, owner)
    initiative = _initiative(world, owner)
    if not staff or initiative is None:  # no document could carry it: no caveat
        return None
    first = max(version.valid_from, start) + _LEAD
    last = min(version.valid_to or end, end) - _TAIL
    span_days = max(0, (last - first).days)
    day = (first + timedelta(days=rng.randint(0, min(span_days, 10)))).date()
    at = datetime.combine(day, time(rng.randint(10, 15), rng.choice((0, 15, 30, 45))))
    if not first <= at <= last:
        at = first
    author = rng.choice(staff)
    key = f"{fact_id}@v{number}"
    event = Event(
        id=f"evt:caveat:{key}",
        type="CaveatNote",
        timestamp=at,
        actors={"author": [author]},
        initiative=initiative,
        project=owner,
        deliverable=Deliverable(kind=KIND, medium="document"),
        payload={
            "topic": f"what applies to the {record.attribute.label} of {record.subject_name}",
            "required_facts": [fact_id],
            "forced_stances": {fact_id: "current"},
            "caveats": {
                fact_id: {"sentence": planned.sentence, "value_after": planned.value_after}
            },
        },
    )
    world.add_node(
        Node(
            f"caveat:{key}",
            N_CAVEAT,
            at,
            props={
                "fact": fact_id,
                "version": number,
                "kind": planned.kind,
                "scope": planned.scope,
                "question": planned.question.format(subject=record.subject_name),
                "sentence": planned.sentence,
                "value_before": version.value,
                "value_after": planned.value_after,
                "canonical_after": value_token(planned.value_after),
                "typed_after": planned.typed,
                "arithmetic": planned.arithmetic,
                "event": event.id,
                "author": author,
                "valid_from": at.isoformat(),
                "valid_to": None if version.valid_to is None else version.valid_to.isoformat(),
            },
        )
    )
    return event
