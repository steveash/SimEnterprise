"""The ``build_software`` playbook plugin (technology vertical, ARCHITECTURE §12.3).

This is the *full* product-engineering playbook the M5 milestone calls for — the
real, registered plugin that lives in the ``playbooks`` package and self-registers
into the process-wide :data:`~enterprise_sim.core.registry.PLAYBOOKS` /
:data:`~enterprise_sim.core.registry.PROCESSES` catalogs as an import side effect
(the discovery contract — see
:func:`enterprise_sim.core.registry.discover`). It is authored entirely through the
declarative SDK (:mod:`enterprise_sim.authoring.sdk`); the engine core is untouched,
which is the whole point of the authoring layer (§14, D24).

It composes **four processes** wired into an event-driven triggering graph:

* :func:`project_kickoff` (``OnStart``) — the lead kicks the project off and the
  squad grooms an initial backlog. Seeds the scenario and announces the
  ``project_kicked_off`` milestone.
* :func:`sprint_cycle` (``OnCadence("per_sprint:2w")``) — plan → build (a
  multi-engineer commit ``Spread``) → sprint review, every two weeks. Each plan
  emits ``SprintPlanned``.
* :func:`weekly_status` (``OnCadence("weekly:FRI")``) — the lead's Friday status
  report; the recurring heartbeat that keeps the corpus plausibly paced.
* :func:`design_review` (``OnEvent("SprintPlanned")``) — the canonical *draft →
  multi-day, multi-reviewer comment window → approve* recipe, fired off each
  sprint's plan; announces ``design_signed_off``.

Unlike the deliberately-minimal teaching version in
:func:`enterprise_sim.authoring.patterns.build_software` (which carries a known
dead-trigger for the linter to surface), this playbook is **lint-clean,
conformance-clean (I1–I8), P-clean (P1–P6), and Tier-3-realistic** — see
``tests/playbooks/test_build_software_playbook.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
    Process,
    Role,
    Selector,
    Spread,
    Step,
)
from enterprise_sim.core.registry import PLAYBOOKS, PROCESSES

__all__ = [
    "build_software",
    "design_review",
    "project_kickoff",
    "sprint_cycle",
    "weekly_status",
]

#: The focal project every activation anchors on (the work is *for* this node).
_PROJECT = "project:checkout"


# --------------------------------------------------------------------------- #
# Scenario roles — shared across the activations (resolved from the KG).
# --------------------------------------------------------------------------- #


def _lead() -> Role:
    """The engineering lead who kicks off, plans, reports, and signs off."""
    return Role(
        name="lead",
        select=Selector(type="Person", where=(Match("role", "eq", "eng_lead"),), count=1),
        description="The engineering lead who plans, reports, and ships.",
    )


def _engineers() -> Role:
    """The product-engineering squad who build the sprint and groom the backlog."""
    return Role(
        name="engineers",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", "engineering"),),
            rank_by=("affinity", "inverse_load"),
            count="3..5",
        ),
        description="The product-engineering squad doing the sprint work.",
    )


def _reviewers() -> Role:
    """Peer reviewers for a design doc, drawn by affinity + load + expertise."""
    return Role(
        name="reviewers",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", "engineering"),),
            rank_by=("affinity", "inverse_load", "expertise"),
            count="2..3",
        ),
        description="Peer reviewers drawn by affinity, load balance, and expertise.",
    )


# --------------------------------------------------------------------------- #
# Processes.
# --------------------------------------------------------------------------- #


def project_kickoff() -> Process:
    """Kick the project off (``OnStart``): align on intent, then groom a backlog.

    Two steps: the lead authors a kickoff brief and announces the
    ``project_kicked_off`` milestone; the squad then grooms an initial backlog,
    each engineer raising a few backlog items (a ``Spread``) threaded back to the
    kickoff. Seeds the scenario.
    """
    return Process(
        name="project_kickoff",
        description="Kick off the project and groom an initial backlog.",
        roles=(_lead(), _engineers()),
        steps=(
            Step(
                id="kickoff",
                by="lead",
                at="day 0",
                duration="1d",
                emits=(
                    EmittedEvent("ProjectKickedOff", payload={"intent": "align on the project"}),
                ),
                produces=Deliverable("kickoff_brief", "document"),
                effects=(KGEffect.milestone("project_kicked_off"),),
            ),
            Step(
                id="groom",
                by="engineers",
                after="kickoff",
                duration="2d",
                emits=(EmittedEvent("BacklogGroomed"),),
                produces=Deliverable("backlog", "document"),
                repeat=Spread(role="engineers", per_actor="1..3", emits="BacklogItemRaised"),
                parent_step="kickoff",
            ),
        ),
        declares=Declares(
            events=("ProjectKickedOff", "BacklogGroomed", "BacklogItemRaised"),
            deliverables=("kickoff_brief", "backlog"),
            effects=("milestone:project_kicked_off",),
        ),
    )


def sprint_cycle() -> Process:
    """Run one two-week sprint (``OnCadence``): plan → build → review.

    The lead plans the sprint (emitting ``SprintPlanned``, which fans out to a
    :func:`design_review`); the squad builds across the sprint window, each
    engineer pushing a handful of commits (a ``Spread``); the lead closes the
    sprint with a review note.
    """
    return Process(
        name="sprint_cycle",
        description="Plan, build, and review one two-week sprint.",
        roles=(_lead(), _engineers()),
        steps=(
            Step(
                id="plan",
                by="lead",
                at="day 0",
                duration="1d",
                emits=(EmittedEvent("SprintPlanned", payload={"intent": "scope the sprint"}),),
                produces=Deliverable("sprint_plan", "document"),
            ),
            Step(
                id="build",
                by="engineers",
                after="plan",
                duration="8d",
                emits=(EmittedEvent("WorkLogged"),),
                repeat=Spread(role="engineers", per_actor="2..4", emits="CommitPushed"),
                parent_step="plan",
            ),
            Step(
                id="review",
                by="lead",
                after="build",
                duration="1d",
                emits=(EmittedEvent("SprintReviewed"),),
                produces=Deliverable("sprint_review_notes", "document"),
                parent_step="build",
            ),
        ),
        declares=Declares(
            events=("SprintPlanned", "WorkLogged", "CommitPushed", "SprintReviewed"),
            deliverables=("sprint_plan", "sprint_review_notes"),
        ),
    )


def weekly_status() -> Process:
    """The lead's Friday status report (``OnCadence("weekly:FRI")``).

    A single-step recurring heartbeat: one status report per week, keeping the
    corpus paced like a real team's weekly rhythm.
    """
    return Process(
        name="weekly_status",
        description="Write the weekly status report.",
        roles=(_lead(),),
        steps=(
            Step(
                id="status",
                by="lead",
                at="day 0",
                duration="1d",
                emits=(
                    EmittedEvent("StatusReported", payload={"intent": "report weekly progress"}),
                ),
                produces=Deliverable("status_report", "document"),
            ),
        ),
        declares=Declares(events=("StatusReported",), deliverables=("status_report",)),
    )


def design_review() -> Process:
    """Draft a design doc and run a multi-day, multi-reviewer review (``OnEvent``).

    The canonical *draft → spread-comment window → approve* recipe: the lead
    drafts, the reviewers post comments spread (and threaded) over a multi-day
    window, and the lead approves — announcing the ``design_signed_off``
    milestone.
    """
    return Process(
        name="design_review",
        description="Draft a design doc and run a multi-day review thread.",
        roles=(_lead(), _reviewers()),
        steps=(
            Step(
                id="draft",
                by="lead",
                at="day 0",
                duration="1d",
                emits=(EmittedEvent("DesignDrafted"),),
                produces=Deliverable("design_doc", "document"),
            ),
            Step(
                id="review",
                by="reviewers",
                after="draft",
                duration="3d",
                emits=(EmittedEvent("ReviewOpened"),),
                repeat=Spread(role="reviewers", per_actor="2..5", emits="CommentPosted"),
                parent_step="draft",
            ),
            Step(
                id="approve",
                by="lead",
                after="review",
                emits=(EmittedEvent("DesignApproved"),),
                effects=(KGEffect.milestone("design_signed_off"),),
                parent_step="review",
            ),
        ),
        declares=Declares(
            events=("DesignDrafted", "ReviewOpened", "CommentPosted", "DesignApproved"),
            deliverables=("design_doc",),
            effects=("milestone:design_signed_off",),
        ),
    )


# --------------------------------------------------------------------------- #
# The playbook — the event-driven composition.
# --------------------------------------------------------------------------- #


def build_software() -> Playbook:
    """The full ``build_software`` playbook: kickoff + cadence sprints/status + reviews.

    Four activations form the triggering graph: kickoff seeds the scenario
    (``OnStart``); sprints and weekly status run on cadence; each sprint plan fans
    out to a design review (``OnEvent("SprintPlanned")``). P-clean: no dead
    triggers, every activation reachable, no cycles, all deliverable expectations
    covered.
    """
    return Playbook(
        name="build_software",
        vertical="technology",
        goal_template="Ship {project} over {n_sprints} two-week sprints, reviewed each sprint.",
        roles=(_lead(), _engineers(), _reviewers()),
        activations=(
            Activation(
                id="kickoff_on_start",
                process=project_kickoff(),
                trigger=OnStart(),
                anchor=_PROJECT,
            ),
            Activation(
                id="sprint_each_cadence",
                process=sprint_cycle(),
                trigger=OnCadence("per_sprint:2w"),
                anchor=_PROJECT,
            ),
            Activation(
                id="status_weekly",
                process=weekly_status(),
                trigger=OnCadence("weekly:FRI"),
                anchor=_PROJECT,
            ),
            Activation(
                id="review_on_sprint",
                process=design_review(),
                trigger=OnEvent("SprintPlanned"),
                anchor=_PROJECT,
            ),
        ),
        deliverable_expectations=(
            "kickoff_brief",
            "backlog",
            "sprint_plan",
            "sprint_review_notes",
            "status_report",
            "design_doc",
        ),
    )


# --------------------------------------------------------------------------- #
# Registration — make the SDK objects discoverable plugins (§4 registries).
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _PlaybookPlugin:
    """A registrable :class:`~enterprise_sim.core.registry.plugins.Playbook`.

    Bridges an SDK :class:`~enterprise_sim.authoring.sdk.Playbook` to the
    string-typed registry protocol: it exposes ``name`` / ``vertical`` /
    ``deliverables`` (the protocol fields) and keeps :attr:`build` — the zero-arg
    factory that reconstructs a fresh authoring object on demand (authoring
    objects are immutable, so callers always get an untouched tree). Not frozen,
    matching the structural-protocol convention used by the archetype specs (the
    protocol fields are settable variables).
    """

    name: str
    vertical: str
    deliverables: Sequence[str]
    build: Callable[[], Playbook]


@dataclass(slots=True)
class _ProcessPlugin:
    """A registrable :class:`~enterprise_sim.core.registry.plugins.Process`.

    Bridges an SDK :class:`~enterprise_sim.authoring.sdk.Process` to the registry
    protocol: ``emits`` is the process's declared event types and ``requests`` its
    declared deliverable kinds (never a file format — the §4 extensibility
    invariant). :attr:`build` is the zero-arg factory for a fresh process.
    """

    name: str
    emits: Sequence[str]
    requests: Sequence[str]
    build: Callable[[], Process]


def _register() -> _PlaybookPlugin:
    """Register the playbook + its processes into the process-wide catalogs.

    Runs once as an import side effect (module import is idempotent, so the
    registries never see a duplicate). Returns the registered playbook plugin.
    """
    for factory in (project_kickoff, sprint_cycle, weekly_status, design_review):
        process = factory()
        PROCESSES.register(
            _ProcessPlugin(
                name=process.name,
                emits=process.declares.events,
                requests=process.declares.deliverables,
                build=factory,
            )
        )
    playbook = build_software()
    plugin = _PlaybookPlugin(
        name=playbook.name,
        vertical=playbook.vertical,
        deliverables=playbook.deliverable_expectations,
        build=build_software,
    )
    PLAYBOOKS.register(plugin)
    return plugin


#: The registered playbook plugin (registration fires on import).
BUILD_SOFTWARE = _register()
