"""Lower the authoring SDK to the engine spec the scheduler runs (ARCHITECTURE §12).

The authoring SDK (:mod:`enterprise_sim.authoring.sdk`) is what an author *writes*;
the engine spec (:mod:`enterprise_sim.core.sim.spec`) is what the scheduler *runs*.
Both deliberately describe the same primitives — the six triggers, a process of
timed steps over bound roles, selectors, effects, conditions, spreads — so the
boundary between them is a **field-for-field lowering pass**, which is exactly what
this module is. The SDK docstring names this the "SDK lowers to the spec" boundary;
keeping it here (rather than on either dataclass) lets the engine evolve its internal
representation without the authoring surface changing.

The lowering is intentionally total and side-effect free: every SDK object maps to
exactly one engine object. A handful of author conveniences the engine has no
concept of are dropped here, each documented at its function:

* a step's multiple :class:`~enterprise_sim.authoring.sdk.EmittedEvent` s collapse
  to the engine's single ``emits`` (the first event's type; its ``payload`` folds
  into the step payload) — the engine emits one event per step;
* a selector's ``external`` flag is a *world-builder* hint, not an engine concept,
  so it is dropped (the test kit reads it when synthesising a world);
* a step's ``when`` guard and a spread's ``over`` window have no engine counterpart
  in v1 and are dropped.

These are the only lossy points and the test kit accounts for each.
"""

from __future__ import annotations

from enterprise_sim.authoring import sdk
from enterprise_sim.core.sim import resolver, spec

__all__ = [
    "lower_activation",
    "lower_condition",
    "lower_effect",
    "lower_playbook",
    "lower_process",
    "lower_role",
    "lower_selector",
    "lower_step",
    "lower_trigger",
]


def lower_match(match: sdk.Match) -> resolver.Filter:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Match` to a resolver ``Filter``."""
    return resolver.Filter(field=match.field, op=match.op, value=match.value)


def lower_selector(selector: sdk.Selector) -> resolver.Selector:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Selector` to the engine one.

    The ``external`` flag is a world-builder hint (materialise an out-of-org party)
    with no meaning to the resolver, so it is dropped; every other field maps
    one-for-one.
    """
    return resolver.Selector(
        type=selector.type,
        where=tuple(lower_match(m) for m in selector.where),
        exclude=tuple(selector.exclude),
        rank_by=tuple(selector.rank_by),  # type: ignore[arg-type]
        expertise=tuple(selector.expertise),
        count=selector.count,
        distinct=selector.distinct,
    )


def lower_role(role: sdk.Role) -> spec.RoleSpec:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Role` to a :class:`RoleSpec`.

    A role with no selector is activation-bound; its engine counterpart simply
    carries ``selector=None`` so the scheduler reads it from ``Activation.bind``.
    """
    return spec.RoleSpec(
        name=role.name,
        selector=lower_selector(role.select) if role.select is not None else None,
    )


def lower_condition(expr: sdk.ConditionExpr) -> spec.Condition:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.ConditionExpr` to a ``Condition``."""
    return spec.Condition(node_id=expr.node, attr=expr.attr, op=expr.op, value=expr.value)


def lower_effect(effect: sdk.KGEffect) -> spec.Effect:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.KGEffect` to an ``Effect``.

    The field set mirrors the engine ``Effect`` exactly, so this is a direct copy
    keyed by :class:`~enterprise_sim.authoring.sdk.EffectKind`.
    """
    return spec.Effect(
        kind=spec.EffectKind(effect.kind.value),
        target=effect.target,
        node_type=effect.node_type,
        edge_type=effect.edge_type,
        src=effect.src,
        dst=effect.dst,
        attr=effect.attr,
        value=effect.value,
        name=effect.name,
        props=dict(effect.props),
    )


def lower_spread(spread: sdk.Spread) -> spec.Spread:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Spread` to the engine ``Spread``.

    The ``over`` window selector is an author convenience (it always names the
    spanning step's ``duration`` in v1) with no engine counterpart, so it is
    dropped; the engine spreads over the step's ``duration`` directly.
    """
    return spec.Spread(
        role=spread.role,
        per_actor=spread.per_actor,
        emits=spread.emits,
        reply_rate=spread.reply_rate,
    )


def lower_step(step: sdk.Step) -> spec.Step:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Step` to an engine ``Step``.

    A step's :class:`~enterprise_sim.authoring.sdk.EmittedEvent` list collapses to
    the engine's single ``emits``: the first event's ``type`` becomes ``emits`` and
    its static ``payload`` folds into the step payload (the engine emits exactly one
    event per step). A step with no emitted event cannot be lowered — the engine has
    no notion of a step that emits nothing — so this raises. The author-only ``when``
    guard is dropped (no engine guard in v1).
    """
    if not step.emits:
        raise ValueError(f"step {step.id!r} emits no event; engine steps must emit one")
    primary = step.emits[0]
    return spec.Step(
        id=step.id,
        emits=primary.type,
        by=step.by,
        at=step.at,
        after=step.after,
        offset=step.offset,
        duration=step.duration,
        slot=step.slot,
        produces=step.produces,
        effects=tuple(lower_effect(e) for e in step.effects),
        spread=lower_spread(step.repeat) if step.repeat is not None else None,
        parent_step=step.parent_step,
        payload=dict(primary.payload),
    )


def lower_process(process: sdk.Process) -> spec.Process:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Process` to an engine ``Process``.

    Only the declarative surface lowers — roles, steps, priority. The ``impl``
    escape hatch, ``declares``, and descriptions are author/contract metadata the
    scheduler never reads, so they do not cross the boundary (the test kit keeps the
    original SDK process alongside for the ``declares`` conformance check). An
    ``impl``-backed process (no steps) lowers to a process with no steps, which the
    engine simply runs to an empty stream — its behaviour is out of scope for the
    declarative engine.
    """
    return spec.Process(
        name=process.name,
        roles=tuple(lower_role(r) for r in process.roles),
        steps=tuple(lower_step(s) for s in process.steps),
        priority=process.priority,
    )


def lower_trigger(trigger: sdk.Trigger) -> spec.Trigger:
    """Lower one of the six SDK triggers to its engine counterpart."""
    if isinstance(trigger, sdk.OnStart):
        return spec.OnStart()
    if isinstance(trigger, sdk.OnCadence):
        return spec.OnCadence(rule=trigger.rule)
    if isinstance(trigger, sdk.OnEvent):
        return spec.OnEvent(
            event_type=trigger.type,
            where=tuple(
                spec.EventPredicate(field=m.field, op=m.op, value=m.value) for m in trigger.where
            ),
        )
    if isinstance(trigger, sdk.OnMilestone):
        return spec.OnMilestone(name=trigger.name)
    if isinstance(trigger, sdk.OnCondition):
        return spec.OnCondition(condition=lower_condition(trigger.expr))
    if isinstance(trigger, sdk.Probabilistic):
        return spec.Probabilistic(rate=trigger.rate, per=trigger.per)
    raise TypeError(f"unknown trigger type: {type(trigger).__name__}")


def lower_activation(activation: sdk.Activation) -> spec.Activation:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Activation` to an engine one."""
    return spec.Activation(
        id=activation.id,
        process=lower_process(activation.process),
        trigger=lower_trigger(activation.trigger),
        bind={role: tuple(ids) for role, ids in activation.bind.items()},
        anchor=activation.anchor,
        params=dict(activation.params),
    )


def lower_playbook(playbook: sdk.Playbook) -> spec.Scenario:
    """Lower an SDK :class:`~enterprise_sim.authoring.sdk.Playbook` to a ``Scenario``.

    A playbook is an activation graph plus scenario metadata; the engine only runs
    the activations, so the scenario takes the playbook name and its lowered
    activations.
    """
    return spec.Scenario(
        name=playbook.name,
        activations=tuple(lower_activation(a) for a in playbook.activations),
    )
