"""Layer B + Layer C: world → scheduler events → grounded markdown corpus (§6, §15-16).

This is the end-to-end render pipeline that turns a populated Layer-A
:class:`~enterprise_sim.core.world.World` into the corpus a run ships:

1. **Layer B (simulate).** Every ``scenario`` initiative the world builder planted
   (:mod:`enterprise_sim.world_builders.builder`) names a registered playbook. For
   each, we fetch the playbook plugin, lower it to an engine
   :class:`~enterprise_sim.core.sim.spec.Scenario`
   (:func:`~enterprise_sim.authoring.lowering.lower_playbook`), re-anchor it onto
   the scenario's concrete project, and run the deterministic
   :class:`~enterprise_sim.core.sim.scheduler.Scheduler` across the config window.
   The scheduler mutates the shared world in place (created nodes + per-person
   calendars) and returns the ordered event journal.

2. **Layer C (produce).** Each event that requested a *deliverable* is rendered by
   the :class:`~enterprise_sim.producers.markdown.MarkdownProducer` against a
   timestamped :class:`WorldView` projection, then applied back to the world so a
   later artifact can cite an earlier one (the verified-reference graph, D16/D32).

**Cache locality (D29).** Producers are driven **clustered by shared prefix** —
all of one scenario's artifacts render consecutively, sharing the stable
company/scenario prompt prefix, and within a scenario in chronological order — so
prompt caching stays warm within its TTL (§16.1). The same clustering is mirrored
on disk: a scenario's files land under ``artifacts/<scenario>/``.

Everything is a pure function of ``(world, config, client)``: scenarios are taken
in id order, events in ``(timestamp, id)`` order, and the world/scheduler are
deterministic in the seed, so the same config + a deterministic backend reproduce
a byte-identical corpus.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from enterprise_sim.authoring.lowering import lower_playbook
from enterprise_sim.authoring.sdk import Playbook as SdkPlaybook
from enterprise_sim.core.config import RunConfig
from enterprise_sim.core.events import EventJournal
from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.registry import PLAYBOOKS, UnknownPluginError, discover
from enterprise_sim.core.registry.binding import BindingMap
from enterprise_sim.core.sim.calendar import WorkingCalendar
from enterprise_sim.core.sim.scheduler import Scheduler, ValidationIssue
from enterprise_sim.core.sim.spec import Activation, Scenario
from enterprise_sim.core.world import Node, World
from enterprise_sim.producers.artifact import ProducedArtifact, apply_to_world
from enterprise_sim.producers.markdown import MarkdownProducer, ProducerContext
from enterprise_sim.producers.word import WordProducer


@runtime_checkable
class _BuildablePlaybook(Protocol):
    """A registered playbook plugin that can rebuild its authoring tree on demand.

    The registry is typed to the lean :class:`~enterprise_sim.core.registry.Playbook`
    protocol (name/vertical/deliverables only); the concrete plugins additionally
    expose this zero-arg ``build`` factory, which is what Layer B needs to lower a
    playbook. Structural-typed so any plugin carrying ``build`` qualifies.
    """

    def build(self) -> SdkPlaybook: ...


__all__ = ["CorpusResult", "RenderEstimate", "build_corpus"]

# KG vocabulary this pipeline reads (mirrors the world builder, §3).
_N_INITIATIVE = "Initiative"
_N_COMPANY = "Company"
_E_UNDER = "under"  # project -> scenario initiative


@dataclass(frozen=True, slots=True)
class RenderEstimate:
    """The pre-render dry-run cost estimate (ARCHITECTURE.md §16.4, D13).

    Computed once the scenarios are scheduled (so the deliverable count is known)
    but *before* any LLM call, this is the gate a large run is checked against: if
    ``estimated_cost_usd`` exceeds the configured ceiling the run aborts here, with
    a clear number, rather than partway through an expensive render.

    Attributes:
        num_artifacts: Total deliverable events that will be rendered.
        estimated_cost_usd: ``num_artifacts`` × per-artifact token estimate, priced.
        input_tokens_each / output_tokens_each / cached_input_tokens_each: The
            per-artifact token assumptions the estimate used (from ``scale`` config).
        model: The model the estimate was priced against.
    """

    num_artifacts: int
    estimated_cost_usd: float
    input_tokens_each: int
    output_tokens_each: int
    cached_input_tokens_each: int
    model: str


@dataclass(frozen=True, slots=True)
class CorpusResult:
    """The output of :func:`build_corpus`: the combined log and rendered files.

    Attributes:
        journal: Every event from every simulated scenario, one append-only log.
        artifacts: The rendered :class:`ProducedArtifact` s, in render order
            (scenario-clustered, then chronological). Empty on a ``dry_run``.
        issues: Soft scheduler validation issues, in scenario order.
        estimate: The pre-render dry-run cost estimate (D13).
    """

    journal: EventJournal
    artifacts: tuple[ProducedArtifact, ...]
    issues: tuple[ValidationIssue, ...] = ()
    estimate: RenderEstimate | None = None


@dataclass(frozen=True, slots=True)
class _ScenarioPlan:
    """One scheduled scenario awaiting render (the unit of render concurrency).

    Carries everything a render task needs and nothing it does not: the scenario's
    own ordered event journal and the cache-friendly :class:`ProducerContext`
    (stable company/scenario prefix + cluster directory). Tasks are independent —
    each renders against a private :meth:`World.copy` — so the render phase fans
    out across plans without sharing mutable state (§16.1, D26).
    """

    initiative_id: str
    journal: EventJournal
    ctx: ProducerContext


def build_corpus(
    world: World,
    config: RunConfig,
    client: LLMClient,
    *,
    calendar: WorkingCalendar | None = None,
    dry_run: bool = False,
) -> CorpusResult:
    """Simulate every scenario in ``world`` and render its full markdown corpus.

    Runs in three phases so the expensive Layer-C render is bounded and gated:

    1. **Schedule (sequential, D26).** Every scenario's deterministic scheduler
       runs in id order, mutating ``world`` in place with its created nodes and
       per-person calendars and yielding its ordered event journal. Scheduling is
       cheap and order-sensitive, so it stays sequential.
    2. **Estimate + gate (D13).** With the frozen schedule the total deliverable
       count is known, so a dry-run cost estimate is computed and checked against
       the configured ceiling *before* any model call — a large run that would
       breach the ceiling raises :class:`CostCeilingExceeded` here. With
       ``dry_run=True`` the function returns after this phase, having rendered
       nothing.
    3. **Render (bounded-concurrent, §16.1).** Scenarios render in parallel,
       capped at ``scale.max_concurrency`` via :meth:`LLMClient.generate_many`.
       Each scenario renders its deliverables sequentially against a private
       :meth:`World.copy` — preserving the intra-scenario reference chain (a later
       artifact can cite an earlier one, D16) and shared-prefix cache locality
       (D29) — and the produced artifacts are merged back into ``world`` in
       deterministic scenario order, so concurrency never changes the result.

    Args:
        world: The populated Layer-A KG (mutated in place by Layer B + C).
        config: The validated run configuration (window + ``scale`` controls).
        client: The LLM client producers render against (a deterministic ``fake``
            client keeps a run network-free and reproducible).
        calendar: Working calendar for all placement arithmetic; a default
            business-hours weekday calendar is used when omitted.
        dry_run: When true, schedule and estimate only — render nothing.
    """
    calendar = calendar or WorkingCalendar()
    start = datetime.combine(config.simulation.period_start, calendar.day_start)
    end = datetime.combine(config.simulation.period_end, calendar.day_end)

    discover("enterprise_sim.playbooks")  # idempotent; populates the PLAYBOOKS catalog.

    company_profile = _company_profile(world)

    # -- Phase 1: schedule every scenario (sequential, deterministic). --------
    journal = EventJournal()
    issues: list[ValidationIssue] = []
    plans: list[_ScenarioPlan] = []

    for initiative in _scenario_initiatives(world):
        scenario = _scenario_for(initiative, world)
        if scenario is None:
            continue

        result = Scheduler(world, calendar, root_seed=config.seed).run(
            scenario, start=start, end=end
        )
        issues.extend(result.issues)
        for event in result.journal.ordered():
            journal.append(event)

        plans.append(
            _ScenarioPlan(
                initiative_id=initiative.id,
                journal=result.journal,
                ctx=ProducerContext(
                    company_profile=company_profile,
                    scenario_context=_scenario_context(initiative),
                    artifacts_dir=f"artifacts/{_slug(initiative.id)}",
                ),
            )
        )

    # -- Phase 2: estimate cost and enforce the ceiling before any call (D13). -
    num_artifacts = sum(_deliverable_count(plan.journal) for plan in plans)
    estimate = _estimate_render(client, config, num_artifacts)

    if dry_run:
        return CorpusResult(
            journal=journal,
            artifacts=(),
            issues=tuple(issues),
            estimate=estimate,
        )

    # -- Phase 3: render scenarios under bounded concurrency, then merge. ------
    rendered = client.generate_many([_render_task(world, plan) for plan in plans])

    artifacts: list[ProducedArtifact] = []
    for scenario_artifacts in rendered:
        apply_to_world(world, scenario_artifacts)
        artifacts.extend(scenario_artifacts)

    return CorpusResult(
        journal=journal,
        artifacts=tuple(artifacts),
        issues=tuple(issues),
        estimate=estimate,
    )


def _deliverable_count(journal: EventJournal) -> int:
    """Number of events in ``journal`` that request a deliverable (render tasks)."""
    return sum(1 for event in journal.ordered() if event.deliverable is not None)


def _estimate_render(client: LLMClient, config: RunConfig, num_artifacts: int) -> RenderEstimate:
    """Price the render up front and trip the ceiling before any call (D13)."""
    scale = config.scale
    estimated = client.dry_run_estimate(
        num_tasks=num_artifacts,
        input_tokens_each=scale.est_input_tokens_per_artifact,
        output_tokens_each=scale.est_output_tokens_per_artifact,
        cached_input_tokens_each=scale.est_cached_input_tokens_per_artifact,
    )
    return RenderEstimate(
        num_artifacts=num_artifacts,
        estimated_cost_usd=estimated,
        input_tokens_each=scale.est_input_tokens_per_artifact,
        output_tokens_each=scale.est_output_tokens_per_artifact,
        cached_input_tokens_each=scale.est_cached_input_tokens_per_artifact,
        model=client.config.model,
    )


def _render_task(
    world: World, plan: _ScenarioPlan
) -> Callable[[LLMClient], list[ProducedArtifact]]:
    """Build the closure that renders one scenario in isolation (§16.1, D26).

    The closure copies the frozen ``world`` so its in-render mutations
    (``apply_to_world`` after each artifact, for the reference chain) never touch
    the shared graph or a sibling scenario — making the render order-independent
    and the result deterministic regardless of how the thread pool schedules it.
    """

    def task(client: LLMClient) -> list[ProducedArtifact]:
        return _render_scenario(world.copy(), plan.journal, client, plan.ctx)

    return task


# --------------------------------------------------------------------------- #
# Layer B — scenario selection + lowering.
# --------------------------------------------------------------------------- #


def _scenario_initiatives(world: World) -> list[Node]:
    """Return the world's ``scenario`` initiatives, in id order (deterministic)."""
    return [
        node
        for node in world.nodes_by_type(_N_INITIATIVE)
        if node.props.get("type") == "scenario" and node.props.get("playbook")
    ]


def _scenario_for(initiative: Node, world: World) -> Scenario | None:
    """Lower the initiative's playbook to a uniquely-namespaced engine scenario.

    The registered playbook is lowered, then re-keyed so two initiatives running
    the *same* playbook never collide: the scenario takes the initiative id as its
    name (the seed sub-stream + event-id namespace), and every activation id is
    prefixed with it. Each activation is re-anchored onto the initiative's concrete
    project (so emitted events and ``expresses`` edges point at a real node) and
    carries the project/initiative ids in its event payload.

    Returns ``None`` when the named playbook is not registered — a soft skip rather
    than failing the whole run.
    """
    name = str(initiative.props["playbook"])
    try:
        plugin = PLAYBOOKS.get(name)
    except UnknownPluginError:
        return None
    if not isinstance(plugin, _BuildablePlaybook):
        return None

    base = lower_playbook(plugin.build())
    anchor = _project_for(initiative, world)
    activations = tuple(_rekey_activation(act, initiative.id, anchor) for act in base.activations)
    return Scenario(name=initiative.id, activations=activations)


def _rekey_activation(act: Activation, scenario_id: str, anchor: str | None) -> Activation:
    """Namespace an activation under its scenario and re-anchor it on the project."""
    params = dict(act.params)
    if anchor is not None:
        params.setdefault("project", anchor)
    params.setdefault("initiative", scenario_id)
    return replace(
        act,
        id=f"{scenario_id}:{act.id}",
        anchor=anchor if anchor is not None else act.anchor,
        params=params,
    )


def _project_for(initiative: Node, world: World) -> str | None:
    """The concrete project anchored under ``initiative`` (its ``under`` source)."""
    incoming = world.in_edges(initiative.id, _E_UNDER)
    if incoming:
        return incoming[0].src
    return None


# --------------------------------------------------------------------------- #
# Layer C — render the deliverable events to grounded markdown.
# --------------------------------------------------------------------------- #


# The ``deliverable.kind → producer`` binding (§4, D4/D5). ``markdown`` is the
# catch-all default; the document kinds the ``word`` producer declares it
# ``handles`` (``status_report``/``design_doc``) are rebound to it, with the
# simulator untouched. Producer instances are stateless and reused across the run.
_PRODUCERS: dict[str, MarkdownProducer | WordProducer] = {
    MarkdownProducer.name: MarkdownProducer(),
    WordProducer.name: WordProducer(),
}
_BINDING = BindingMap(default=MarkdownProducer.name)
for _kind in WordProducer.handles:
    _BINDING.bind(_kind, WordProducer.name)


def _producer_for(kind: str) -> MarkdownProducer | WordProducer:
    """Resolve a deliverable kind to the producer that renders it (binding map, D4)."""
    return _PRODUCERS[_BINDING.producer_names(kind)[0]]


def _render_scenario(
    world: World,
    journal: EventJournal,
    client: LLMClient,
    ctx: ProducerContext,
) -> list[ProducedArtifact]:
    """Render one scenario's deliverable events, applying each back to the world.

    Each deliverable kind is routed through the binding map to its producer
    (document kinds → ``word`` ``.docx``; everything else → ``markdown``). Only
    events that requested a deliverable become files; comments/commits and
    milestone-only steps live on as threading + KG facts. Events render in
    ``(timestamp, id)`` order and each artifact is applied to the world before the
    next renders, so a later artifact can cite an earlier one (D16/D32).
    """
    rendered: list[ProducedArtifact] = []
    for event in journal.ordered():
        if event.deliverable is None:
            continue
        view = world.projection(at=event.timestamp)
        producer = _producer_for(event.deliverable.kind)
        produced = producer.produce(event, view, client, ctx)
        apply_to_world(world, [produced])
        rendered.append(produced)
    return rendered


# --------------------------------------------------------------------------- #
# Stable prompt-prefix blocks (the cacheable D29 prefix).
# --------------------------------------------------------------------------- #


def _company_profile(world: World) -> str:
    """The company-wide stable prompt block (cached across every artifact, §16.1)."""
    companies = world.nodes_by_type(_N_COMPANY)
    if not companies:
        return ""
    node = companies[0]
    name = node.props.get("name", node.id)
    vertical = node.props.get("vertical", "")
    size = node.props.get("size", "")
    line = f"Company: {name}"
    if vertical or size:
        line += f" ({', '.join(p for p in (vertical, size) if p)})"
    description = node.props.get("description")
    if description:
        line += f". {description}"
    return line


def _scenario_context(initiative: Node) -> str:
    """The per-scenario stable prompt block (cached across its artifacts, §16.1)."""
    name = initiative.props.get("name", initiative.id)
    playbook = initiative.props.get("playbook", "")
    block = f"Scenario: {name}"
    if playbook:
        block += f" (playbook: {playbook})"
    return block + "."


def _slug(value: str) -> str:
    """A filesystem-safe lowercase slug (shared shape with the producer's slugger)."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "scenario"
