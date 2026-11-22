"""The fact ledger: per-project facts that change over time, and who states what.

:func:`build_ledger` gives every project ``facts.per_project`` facts drawn from
its department's attributes (:mod:`enterprise_sim.facts.catalog`). Each fact
has one or more **versions** with a valid-time interval: version 1 holds from
the window start; at each of up to ``max_versions - 1`` draws the fact changes
(probability ``change_rate`` scaled by the attribute's volatility) at a seeded
moment, so later documents should state the new value. With probability
``cross_team`` a project also **depends on** a project of another department,
and its documents then cite that project's launch date and status too.

:func:`assign_assertions` walks every deliverable event in render order and
decides which facts each document states, and how:

* ``update`` — the first document of the owning project after a change
  announces it ("moved from A to B");
* ``stale`` — a superseded value, stated as if current (more often for facts a
  project cites from another team: ``cross_team_stale_boost``);
* ``conflicting`` — a plausible value that was never true;
* ``hedged`` — the true value with a hedge ("I believe ...");
* ``current`` — the true value.

The assertions ride on the event (``payload['facts']``), so every producer's
brief carries them through one path (:func:`~enterprise_sim.core.llm.brief.facts_block_for`),
and :func:`ledger_graph` / :func:`assertion_edges` write the answer key into the
gold KG: ``Fact`` and ``FactVersion`` nodes and ``asserts`` edges from each
artifact, labelled with the stance, the stated value and the version true at
the time. Everything is a pure function of ``(world, config, seed)``.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from enterprise_sim.core.config.diversity import FactsConfig
from enterprise_sim.core.config.seed import SeedContext
from enterprise_sim.core.events import Event
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.facts.catalog import Attribute, Context, attributes_for, value_token

__all__ = [
    "Assertion",
    "FactRecord",
    "Ledger",
    "STANCES",
    "Version",
    "assertion_edges",
    "assign_assertions",
    "build_ledger",
    "ledger_graph",
]

N_FACT = "Fact"
N_FACT_VERSION = "FactVersion"
E_ABOUT = "about"  # fact -> its subject (a project)
E_VERSION_OF = "version_of"  # fact version -> fact
E_SUPERSEDES = "supersedes"  # fact version -> the version it replaced
E_DEPENDS_ON = "depends_on"  # project -> project of another department
E_ASSERTS = "asserts"  # artifact -> fact (props: stance, value, versions)

STANCES: tuple[str, ...] = ("current", "update", "hedged", "stale", "conflicting")


@dataclass(frozen=True, slots=True)
class Version:
    """One value of a fact and the valid-time interval it held."""

    number: int
    value: str
    valid_from: datetime
    valid_to: datetime | None  # exclusive; None = still true at the window end


@dataclass(slots=True)
class FactRecord:
    """A fact about one project and its versions (oldest first)."""

    id: str
    subject: str  # project node id
    subject_name: str
    department: str
    attribute: Attribute
    versions: list[Version] = field(default_factory=list)
    announced: set[int] = field(default_factory=set)
    #: every value the fact takes over the whole horizon, beyond the window too:
    #: a "conflicting" value is one outside this set, so extending the window
    #: never turns a past wrong statement into a merely early one.
    horizon_values: set[str] = field(default_factory=set)

    def version_at(self, at: datetime) -> Version:
        current = self.versions[0]
        for version in self.versions:
            if version.valid_from <= at:
                current = version
        return current

    def version_id(self, number: int) -> str:
        return f"{self.id}@v{number}"


@dataclass(frozen=True, slots=True)
class Assertion:
    """What one document states about one fact."""

    fact: str
    stance: str
    value: str
    statement: str
    true_version: int
    stated_version: int | None  # None for a conflicting (never true) value
    cited: bool  # the fact belongs to another project this one depends on

    def to_payload(self) -> dict[str, Any]:
        return {
            "fact": self.fact,
            "stance": self.stance,
            "value": self.value,
            "statement": self.statement,
            "true_version": self.true_version,
            "stated_version": self.stated_version,
            "cited": self.cited,
        }


@dataclass(slots=True)
class Ledger:
    """Every fact of a world, by project, plus the cross-team dependencies."""

    facts: dict[str, FactRecord] = field(default_factory=dict)
    by_project: dict[str, list[str]] = field(default_factory=dict)
    depends_on: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.facts)


# -- building ----------------------------------------------------------------- #

_CITED = ("target_date", "status")


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")


def _people_of(world: World, department: str) -> tuple[str, ...]:
    """The department's founding staff (successors excluded).

    Successors exist only once their join date is inside the window, so drawing
    owner values from them would make a past fact depend on the window end.
    """
    names = [
        str(n.props.get("name"))
        for n in world.nodes_by_type("Person")
        if n.props.get("department") == department
        and n.props.get("name")
        and "joined_at" not in n.props
    ]
    return tuple(sorted(names))


#: Change moments are drawn over this horizon from the ledger start, not up to
#: the window end, so a fact's history is the same whatever the window: extending
#: a world reveals later versions and never rewrites earlier ones.
_HORIZON = timedelta(days=182)
_GAP = timedelta(days=5)


def _change_moments(
    rng: random.Random, cfg: FactsConfig, attr: Attribute, start: datetime
) -> list[datetime]:
    moments: list[datetime] = []
    floor = start + _GAP
    per_draw = _HORIZON / max(1, cfg.max_versions - 1)
    for _ in range(cfg.max_versions - 1):
        changes = rng.random() < min(1.0, cfg.change_rate * attr.volatile)
        offset = rng.randint(0, int(per_draw.total_seconds() // 3600))
        moment = datetime.combine((floor + timedelta(hours=offset)).date(), time(9, 0))
        if changes:
            moments.append(moment)
        floor = max(floor, moment) + _GAP
    return moments


def build_ledger(world: World, cfg: FactsConfig, *, seed: int, start: date, end: date) -> Ledger:
    """Draw every project's facts and their versions over ``[start, end]``."""
    ledger = Ledger()
    if cfg.per_project <= 0:
        return ledger
    ctx_seed = SeedContext(seed)
    t0 = datetime.combine(start, time(9, 0))
    t1 = datetime.combine(end, time(17, 0))
    projects = sorted(world.nodes_by_type("Project"), key=lambda n: n.id)
    for project in projects:
        department = str(project.props.get("department", ""))
        name = str(project.props.get("name", project.id))
        rng = ctx_seed.rng("facts", project.id)
        pool = list(attributes_for(department))
        picked = set(rng.sample(range(len(pool)), k=min(cfg.per_project, len(pool))))
        chosen = [attr for i, attr in enumerate(pool) if i in picked]
        ctx = Context(start=start, end=end, people=_people_of(world, department))
        ids: list[str] = []
        for attr in chosen:
            fact_id = f"fact:{_slug(project.id.split(':', 1)[-1])}:{attr.name}"
            record = FactRecord(fact_id, project.id, name, department, attr)
            value = attr.draw(rng, ctx)
            moments = _change_moments(rng, cfg, attr, t0)
            froms = [t0, *moments]
            for i, valid_from in enumerate(froms):
                if i > 0:
                    value = attr.mutate(rng, value, ctx)
                record.horizon_values.add(value)
                if valid_from > t1:
                    continue  # a later version: drawn (stable), not yet in the window
                following = froms[i + 1] if i + 1 < len(froms) else None
                valid_to = following if following is not None and following <= t1 else None
                record.versions.append(Version(i + 1, value, valid_from, valid_to))
            ledger.facts[fact_id] = record
            ids.append(fact_id)
        ledger.by_project[project.id] = ids
    by_dept: dict[str, list[str]] = {}
    for project in projects:
        by_dept.setdefault(str(project.props.get("department", "")), []).append(project.id)
    for project in projects:
        rng = ctx_seed.rng("facts-deps", project.id)
        dept = str(project.props.get("department", ""))
        others = sorted(p for d, ps in by_dept.items() if d != dept for p in ps)
        if others and rng.random() < cfg.cross_team:
            ledger.depends_on[project.id] = [rng.choice(others)]
    return ledger


# -- assigning ---------------------------------------------------------------- #


def _distractor(rng: random.Random, record: FactRecord, ctx: Context, true: Version) -> str:
    """A plausible value near the one true at the time, never true over the horizon."""
    seen = record.horizon_values | {v.value for v in record.versions}
    value = true.value
    for _ in range(8):
        value = record.attribute.mutate(rng, value, ctx)
        if value not in seen:
            return value
    return value


def _pool(ledger: Ledger, project: str | None) -> list[tuple[str, bool]]:
    if project is None:
        return []
    own = [(f, False) for f in ledger.by_project.get(project, [])]
    cited = [
        (f, True)
        for dep in ledger.depends_on.get(project, [])
        for f in ledger.by_project.get(dep, [])
        if ledger.facts[f].attribute.name in _CITED
    ]
    return own + cited


def assign_assertions(
    ledger: Ledger,
    events: Iterable[Event],
    cfg: FactsConfig,
    *,
    seed: int,
    people_of: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, list[Assertion]]:
    """Decide each deliverable event's assertions (events in render order).

    Mutates ``ledger`` (which changes have been announced) and returns the
    assertions by event id; call :func:`attach` to put them on the events.
    """
    out: dict[str, list[Assertion]] = {}
    if not ledger:
        return out
    lo, hi = cfg.per_artifact_range
    ctx_seed = SeedContext(seed)
    for event in events:
        if event.deliverable is None:
            continue
        pool = _pool(ledger, event.project)
        if not pool:
            continue
        rng = ctx_seed.rng("assert", event.id)
        at = event.timestamp
        pending: list[tuple[str, bool]] = [
            (f, c)
            for f, c in pool
            if not c
            and (v := ledger.facts[f].version_at(at)).number > 1
            and v.number not in ledger.facts[f].announced
        ]
        count = min(len(pool), rng.randint(lo, hi))
        chosen: list[tuple[str, bool]] = pending[:count]
        rest = [p for p in pool if p not in chosen]
        chosen += rng.sample(rest, k=min(len(rest), count - len(chosen)))
        assertions: list[Assertion] = []
        for fact_id, cited in chosen:
            record = ledger.facts[fact_id]
            ctx = Context(
                start=at.date(),
                end=at.date(),
                people=(people_of or {}).get(record.department, ()),
            )
            assertions.append(_assert(rng, record, cfg, at=at, cited=cited, ctx=ctx))
        if assertions:
            out[event.id] = assertions
    return out


def _assert(
    rng: random.Random,
    record: FactRecord,
    cfg: FactsConfig,
    *,
    at: datetime,
    cited: bool,
    ctx: Context,
) -> Assertion:
    attr = record.attribute
    true = record.version_at(at)
    subject = record.subject_name
    template = rng.choice(attr.current)

    def made(stance: str, value: str, statement: str, stated: int | None) -> Assertion:
        return Assertion(record.id, stance, value, statement, true.number, stated, cited)

    if not cited and true.number > 1 and true.number not in record.announced:
        record.announced.add(true.number)
        old = record.versions[true.number - 2].value
        text = attr.state(attr.update, subject=subject, value=true.value, old=old)
        return made("update", true.value, text, true.number)
    roll = rng.random()
    stale_p = cfg.stale_rate * (cfg.cross_team_stale_boost if cited else 1.0)
    if true.number > 1 and roll < min(1.0, stale_p):
        old_version = rng.choice(record.versions[: true.number - 1])
        text = attr.state(template, subject=subject, value=old_version.value)
        return made("stale", old_version.value, text, old_version.number)
    roll = rng.random()
    if roll < cfg.conflict_rate:
        wrong = _distractor(rng, record, ctx, true)
        if wrong not in record.horizon_values and all(wrong != v.value for v in record.versions):
            return made(
                "conflicting", wrong, attr.state(template, subject=subject, value=wrong), None
            )
    roll = rng.random()
    text = attr.state(template, subject=subject, value=true.value)
    if roll < cfg.hedge_rate:
        hedge = rng.choice(attr.hedges)
        opener = text if template.startswith("{") else text[:1].lower() + text[1:]
        return made("hedged", true.value, f"{hedge} {opener}", true.number)
    return made("current", true.value, text, true.number)


def attach(events: Iterable[Event], assertions: dict[str, list[Assertion]]) -> None:
    """Put each event's assertions on its payload (``payload['facts']``)."""
    for event in events:
        found = assertions.get(event.id)
        if found:
            event.payload["facts"] = [a.to_payload() for a in found]


# -- the gold KG --------------------------------------------------------------- #


def ledger_graph(world: World, ledger: Ledger) -> None:
    """Write the ledger into ``world``: facts, versions, dependencies."""
    for record in ledger.facts.values():
        first = record.versions[0].valid_from
        world.add_node(
            Node(
                record.id,
                N_FACT,
                first,
                props={
                    "subject": record.subject,
                    "attribute": record.attribute.name,
                    "label": record.attribute.label,
                    "question": record.attribute.question.format(subject=record.subject_name),
                    "versions": len(record.versions),
                },
                aliases=[],
            )
        )
        world.add_edge(
            Edge(
                f"edge:{E_ABOUT}:{record.id}->{record.subject}",
                E_ABOUT,
                record.id,
                record.subject,
                first,
            )
        )
        previous: str | None = None
        for version in record.versions:
            vid = record.version_id(version.number)
            world.add_node(
                Node(
                    vid,
                    N_FACT_VERSION,
                    version.valid_from,
                    props={
                        "fact": record.id,
                        "version": version.number,
                        "value": version.value,
                        "token": value_token(version.value),
                        "valid_from": version.valid_from.isoformat(),
                        "valid_to": version.valid_to.isoformat() if version.valid_to else None,
                    },
                    aliases=[],
                )
            )
            world.add_edge(
                Edge(
                    f"edge:{E_VERSION_OF}:{vid}->{record.id}",
                    E_VERSION_OF,
                    vid,
                    record.id,
                    version.valid_from,
                )
            )
            if previous is not None:
                world.add_edge(
                    Edge(
                        f"edge:{E_SUPERSEDES}:{vid}->{previous}",
                        E_SUPERSEDES,
                        vid,
                        previous,
                        version.valid_from,
                    )
                )
            previous = vid
    for project, deps in sorted(ledger.depends_on.items()):
        node = world.get_node(project)
        at = node.created_at if node is not None else datetime.min
        for dep in deps:
            world.add_edge(
                Edge(f"edge:{E_DEPENDS_ON}:{project}->{dep}", E_DEPENDS_ON, project, dep, at)
            )


def assertion_edges(artifact_id: str, event: Event) -> list[Edge]:
    """The ``asserts`` edges for an artifact rendered from ``event``."""
    edges: list[Edge] = []
    for fact in event.payload.get("facts") or []:
        edges.append(
            Edge(
                f"edge:{E_ASSERTS}:{artifact_id}->{fact['fact']}",
                E_ASSERTS,
                artifact_id,
                str(fact["fact"]),
                event.timestamp,
                props={
                    "stance": fact["stance"],
                    "value": fact["value"],
                    "true_version": fact["true_version"],
                    "stated_version": fact["stated_version"],
                    "cited": fact["cited"],
                },
            )
        )
    return edges
