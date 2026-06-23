"""Tests for the SDK → engine-spec lowering pass (ARCHITECTURE §12, esim-bb00bb20).

The test kit runs SDK objects by lowering them to the engine spec the scheduler
consumes; these tests pin that lowering is a faithful, field-for-field copy and that
the three documented lossy points (multi-emit collapse, dropped ``external``/``when``/
``over``) behave as specified.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from enterprise_sim.authoring import lowering, sdk
from enterprise_sim.authoring.patterns import build_software, run_clinical_study, sell_merchandise
from enterprise_sim.core.sim import spec
from enterprise_sim.core.sim.resolver import Filter


def test_lower_match_is_field_for_field() -> None:
    m = sdk.Match("team", "eq", "engineering")
    assert lowering.lower_match(m) == Filter("team", "eq", "engineering")


def test_lower_selector_drops_external_keeps_rest() -> None:
    sel = sdk.Selector(
        type="Person",
        where=(sdk.Match("team", "eq", "eng"),),
        exclude=("p:a",),
        rank_by=("affinity", "inverse_load"),
        expertise=("payments",),
        count="2..3",
        distinct=False,
        external=True,
    )
    lowered = lowering.lower_selector(sel)
    assert lowered.type == "Person"
    assert lowered.where == (Filter("team", "eq", "eng"),)
    assert lowered.exclude == ("p:a",)
    assert lowered.rank_by == ("affinity", "inverse_load")
    assert lowered.expertise == ("payments",)
    assert lowered.count == "2..3"
    assert lowered.distinct is False
    # `external` has no engine field — it must not survive lowering.
    assert not hasattr(lowered, "external")


def test_lower_role_handles_bound_and_resolved() -> None:
    bound = lowering.lower_role(sdk.Role(name="author"))
    assert bound == spec.RoleSpec(name="author", selector=None)
    resolved = lowering.lower_role(
        sdk.Role(name="reviewers", select=sdk.Selector(type="Person", count=2))
    )
    assert resolved.name == "reviewers"
    assert resolved.selector is not None and resolved.selector.count == 2


def test_lower_condition() -> None:
    expr = sdk.ConditionExpr(node="sku:widget", attr="stock_level", op="lte", value=10)
    assert lowering.lower_condition(expr) == spec.Condition("sku:widget", "stock_level", "lte", 10)


@pytest.mark.parametrize(
    "effect",
    [
        sdk.KGEffect.create("n:1", "Doc", {"k": "v"}),
        sdk.KGEffect.mutate("study:7", "stage", "approved"),
        sdk.KGEffect.add_edge("e:1", "reviews", "a", "b"),
        sdk.KGEffect.milestone("done"),
    ],
)
def test_lower_effect_preserves_all_fields(effect: sdk.KGEffect) -> None:
    lowered = lowering.lower_effect(effect)
    assert lowered.kind.value == effect.kind.value
    assert lowered.target == effect.target
    assert lowered.node_type == effect.node_type
    assert lowered.edge_type == effect.edge_type
    assert lowered.src == effect.src
    assert lowered.dst == effect.dst
    assert lowered.attr == effect.attr
    assert lowered.value == effect.value
    assert lowered.name == effect.name
    assert dict(lowered.props) == dict(effect.props)


def test_lower_step_collapses_first_emit_and_folds_payload() -> None:
    step = sdk.Step(
        id="s",
        by="lead",
        at="day 1",
        duration="2d",
        emits=(
            sdk.EmittedEvent("Primary", payload={"intent": "go"}),
            sdk.EmittedEvent("Secondary"),
        ),
    )
    lowered = lowering.lower_step(step)
    assert lowered.emits == "Primary"
    assert lowered.payload == {"intent": "go"}
    assert lowered.by == "lead"
    assert lowered.at == "day 1"
    assert lowered.duration == "2d"


def test_lower_step_carries_spread_from_repeat() -> None:
    step = sdk.Step(
        id="s",
        emits=(sdk.EmittedEvent("Opened"),),
        repeat=sdk.Spread(
            role="reviewers", per_actor="2..4", emits="CommentPosted", reply_rate=0.3
        ),
    )
    lowered = lowering.lower_step(step)
    assert lowered.spread == spec.Spread(
        role="reviewers", per_actor="2..4", emits="CommentPosted", reply_rate=0.3
    )


def test_lower_step_requires_an_emit() -> None:
    with pytest.raises(ValueError, match="must emit"):
        lowering.lower_step(sdk.Step(id="s"))


@pytest.mark.parametrize(
    ("trigger", "expected_type"),
    [
        (sdk.OnStart(), spec.OnStart),
        (sdk.OnCadence("weekly:FRI"), spec.OnCadence),
        (sdk.OnEvent("X"), spec.OnEvent),
        (sdk.OnMilestone("m"), spec.OnMilestone),
        (sdk.OnCondition(sdk.ConditionExpr("n", "a", "eq", 1)), spec.OnCondition),
        (sdk.Probabilistic(rate=2.0, per="day"), spec.Probabilistic),
    ],
)
def test_lower_trigger_maps_each_of_the_six(trigger: sdk.Trigger, expected_type: type) -> None:
    assert isinstance(lowering.lower_trigger(trigger), expected_type)


def test_lower_onevent_lowers_where_predicates() -> None:
    trig = sdk.OnEvent("LowStock", where=(sdk.Match("payload.sku", "eq", "widget"),))
    lowered = lowering.lower_trigger(trig)
    assert isinstance(lowered, spec.OnEvent)
    assert lowered.event_type == "LowStock"
    assert lowered.where == (spec.EventPredicate("payload.sku", "eq", "widget"),)


@pytest.mark.parametrize("builder", [build_software, sell_merchandise, run_clinical_study])
def test_reference_playbooks_lower_without_error(builder: Callable[[], sdk.Playbook]) -> None:
    scenario = lowering.lower_playbook(builder())
    assert isinstance(scenario, spec.Scenario)
    assert scenario.activations  # every reference playbook has activations
    # Every lowered activation carries a lowered process + one of the six triggers.
    for act in scenario.activations:
        assert isinstance(act.process, spec.Process)
        assert isinstance(
            act.trigger,
            (
                spec.OnStart,
                spec.OnCadence,
                spec.OnEvent,
                spec.OnMilestone,
                spec.OnCondition,
                spec.Probabilistic,
            ),
        )
