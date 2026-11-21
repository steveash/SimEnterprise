"""Department kits: an archetype and its playbook from one declarative table.

The two reference departments (``engineering``, ``retail``) hand-write their
playbooks. Most departments of a real company share one shape — a lead who
kicks work off and reports on a cadence, members who write recurring documents,
reviewers who comment, and work that arrives at random (incidents, requests,
escalations). :class:`DepartmentKit` declares that shape as data, and
:func:`register_kit` turns it into a registered
:class:`~enterprise_sim.archetypes._base.DepartmentArchetypeSpec` plus an SDK
:class:`~enterprise_sim.authoring.sdk.Playbook` (``run_<name>``) whose processes
are named ``<name>__<doc id>`` so kits never collide in the process registry.

Each :class:`DocSpec` is one recurring document:

* ``trigger`` — ``start``; ``cadence:<rule>`` (``weekly:MON``, ``every:2w``,
  ``daily:workdays``); ``event:<EventType>`` (fires after another doc of the kit
  emits that event); or ``random:<rate>/<day|week|sprint|month>``;
* ``by`` — ``lead`` (the department head) or ``member`` (one member, ranked by
  inverse load, so authorship spreads);
* ``review`` — adds a 2-day multi-reviewer comment window and a lead approval.

The kits live in ``templates/<name>/plugin.py`` like every other template.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from enterprise_sim.archetypes._base import DepartmentArchetypeSpec, TeamShape
from enterprise_sim.authoring.sdk import (
    Activation,
    Declares,
    Deliverable,
    EmittedEvent,
    KGEffect,
    Match,
    OnCadence,
    OnEvent,
    OnStart,
    Playbook,
    Probabilistic,
    Process,
    Role,
    Selector,
    Spread,
    Step,
)
from enterprise_sim.core.registry import ARCHETYPES, PLAYBOOKS, PROCESSES

__all__ = ["DepartmentKit", "DocSpec", "RegisteredKit", "register_kit"]

_ANCHOR = "project:main"


@dataclass(frozen=True, slots=True)
class DocSpec:
    """One recurring document of a department (see the module docstring)."""

    id: str
    kind: str
    intent: str
    trigger: str
    event: str
    by: str = "lead"
    review: bool = False
    milestone: str | None = None


@dataclass(frozen=True, slots=True)
class DepartmentKit:
    """A department archetype and the documents its playbook produces."""

    name: str
    charter: str
    typical_goals: tuple[str, ...]
    team_shapes: tuple[TeamShape, ...]
    lead_role: str
    docs: tuple[DocSpec, ...]
    vertical: str = "operations"
    verticals: tuple[str, ...] = ()
    ic_noun: str | None = None

    @property
    def playbook_name(self) -> str:
        return f"run_{self.name}"


@dataclass(slots=True)
class _PlaybookPlugin:
    name: str
    vertical: str
    deliverables: Sequence[str]
    build: Callable[[], Playbook]


@dataclass(slots=True)
class _ProcessPlugin:
    name: str
    emits: Sequence[str]
    requests: Sequence[str]
    build: Callable[[], Process]


@dataclass(frozen=True, slots=True)
class RegisteredKit:
    """What :func:`register_kit` registered."""

    archetype: DepartmentArchetypeSpec
    playbook: _PlaybookPlugin


def _lead(kit: DepartmentKit) -> Role:
    return Role(
        name="lead",
        select=Selector(type="Person", where=(Match("role", "eq", kit.lead_role),), count=1),
        description=f"The {kit.name.replace('_', ' ')} lead.",
    )


def _member(kit: DepartmentKit) -> Role:
    return Role(
        name="member",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", kit.name),),
            rank_by=("inverse_load", "affinity"),
            count=1,
        ),
        description="One department member, chosen to spread the load.",
    )


def _reviewers(kit: DepartmentKit) -> Role:
    return Role(
        name="reviewers",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", kit.name),),
            rank_by=("affinity", "inverse_load", "expertise"),
            count="2..3",
        ),
        description="Peer reviewers from the department.",
    )


def _process(kit: DepartmentKit, doc: DocSpec) -> Process:
    author = "lead" if doc.by == "lead" else "member"
    roles = [_lead(kit)]
    if author == "member":
        roles.append(_member(kit))
    effects = (KGEffect.milestone(doc.milestone),) if doc.milestone else ()
    steps = [
        Step(
            id="write",
            by=author,
            at="day 0",
            duration="1d",
            emits=(EmittedEvent(doc.event, payload={"intent": doc.intent}),),
            produces=Deliverable(doc.kind, "document"),
            effects=() if doc.review else effects,
        )
    ]
    events = [doc.event]
    if doc.review:
        roles.append(_reviewers(kit))
        steps.append(
            Step(
                id="review",
                by="reviewers",
                after="write",
                duration="2d",
                emits=(EmittedEvent(f"{doc.event}ReviewOpened"),),
                repeat=Spread(role="reviewers", per_actor="1..3", emits=f"{doc.event}Comment"),
                parent_step="write",
            )
        )
        steps.append(
            Step(
                id="approve",
                by="lead",
                after="review",
                emits=(EmittedEvent(f"{doc.event}Approved"),),
                effects=effects,
                parent_step="review",
            )
        )
        events += [f"{doc.event}ReviewOpened", f"{doc.event}Comment", f"{doc.event}Approved"]
    return Process(
        name=f"{kit.name}__{doc.id}",
        description=f"{doc.kind.replace('_', ' ')}: {doc.intent}.",
        roles=tuple(roles),
        steps=tuple(steps),
        declares=Declares(
            events=tuple(events),
            deliverables=(doc.kind,),
            effects=tuple(f"milestone:{doc.milestone}" for _ in (0,) if doc.milestone),
        ),
    )


def _trigger(spec: str) -> OnStart | OnCadence | OnEvent | Probabilistic:
    kind, _, arg = spec.partition(":")
    if kind == "start":
        return OnStart()
    if kind == "cadence":
        return OnCadence(arg)
    if kind == "event":
        return OnEvent(arg)
    if kind == "random":
        rate, _, per = arg.partition("/")
        if per not in ("day", "week", "sprint", "month"):
            raise ValueError(f"random trigger period must be day/week/sprint/month: {spec!r}")
        return Probabilistic(rate=float(rate), per=per)  # type: ignore[arg-type]
    raise ValueError(f"unknown trigger {spec!r}")


def _playbook(kit: DepartmentKit) -> Playbook:
    roles = [_lead(kit), _member(kit), _reviewers(kit)]
    return Playbook(
        name=kit.playbook_name,
        vertical=kit.vertical,
        goal_template=f"Run the {kit.name.replace('_', ' ')} function for {{project}}.",
        roles=tuple(roles),
        activations=tuple(
            Activation(
                id=doc.id,
                process=_process(kit, doc),
                trigger=_trigger(doc.trigger),
                anchor=_ANCHOR,
            )
            for doc in kit.docs
        ),
        deliverable_expectations=tuple(dict.fromkeys(doc.kind for doc in kit.docs)),
    )


def register_kit(kit: DepartmentKit) -> RegisteredKit:
    """Register ``kit``'s archetype, playbook and processes (idempotent per import)."""
    for doc in kit.docs:
        process = _process(kit, doc)

        def build(kit: DepartmentKit = kit, doc: DocSpec = doc) -> Process:
            return _process(kit, doc)

        PROCESSES.register(
            _ProcessPlugin(
                name=process.name,
                emits=process.declares.events,
                requests=process.declares.deliverables,
                build=build,
            )
        )
    playbook = _playbook(kit)
    plugin = _PlaybookPlugin(
        name=playbook.name,
        vertical=playbook.vertical,
        deliverables=playbook.deliverable_expectations,
        build=lambda: _playbook(kit),
    )
    PLAYBOOKS.register(plugin)
    spec = DepartmentArchetypeSpec(
        name=kit.name,
        charter=kit.charter,
        typical_goals=kit.typical_goals,
        team_shapes=kit.team_shapes,
        playbooks=(kit.playbook_name,),
        lead_role=kit.lead_role,
        verticals=kit.verticals,
        ic_noun=kit.ic_noun,
    )
    ARCHETYPES.register(spec)
    return RegisteredKit(spec, plugin)
