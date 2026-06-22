"""Tests for the declarative authoring SDK (ARCHITECTURE §12, esim-3c9cfc55).

Acceptance: the three cross-vertical reference patterns (§12.3) are expressible
as objects and **round-trip** through ``to_dict`` / ``from_dict``. We also cover
each primitive and all six triggers individually, plus JSON-stability (the
serialized form is real JSON), so the round-trip contract holds end to end.
"""

from __future__ import annotations

import json

import pytest
from enterprise_sim.authoring import (
    REFERENCE_PLAYBOOKS,
    Activation,
    ConditionExpr,
    Declares,
    Deliverable,
    EffectKind,
    EmittedEvent,
    KGEffect,
    Match,
    OnCadence,
    OnCondition,
    OnEvent,
    OnMilestone,
    OnStart,
    Playbook,
    Probabilistic,
    Process,
    Role,
    Selector,
    Spread,
    Step,
    build_software,
    run_clinical_study,
    sell_merchandise,
    trigger_from_dict,
)
from enterprise_sim.authoring.sdk import Trigger

# --------------------------------------------------------------------------- #
# Per-primitive round-trips.
# --------------------------------------------------------------------------- #


def test_match_round_trips() -> None:
    m = Match("team", "eq", "engineering")
    assert Match.from_dict(m.to_dict()) == m


def test_selector_round_trips_with_all_fields() -> None:
    sel = Selector(
        type="Person",
        where=(Match("team", "eq", "eng"), Match("seniority", "gte", 3)),
        exclude=("p1",),
        rank_by=("affinity", "inverse_load"),
        expertise=("payments",),
        count="2..3",
        distinct=False,
        external=False,
    )
    assert Selector.from_dict(sel.to_dict()) == sel


def test_selector_external_flag_round_trips() -> None:
    sel = Selector(type="Supplier", external=True, count=1)
    restored = Selector.from_dict(sel.to_dict())
    assert restored == sel
    assert restored.external is True


def test_role_with_and_without_selector_round_trip() -> None:
    bound = Role(name="author")  # activation-bound, no selector
    resolved = Role(name="reviewers", select=Selector(type="Person", count="1..2"), description="x")
    assert Role.from_dict(bound.to_dict()) == bound
    assert Role.from_dict(resolved.to_dict()) == resolved
    assert bound.select is None


def test_emitted_event_round_trips() -> None:
    ev = EmittedEvent("DesignDrafted", payload={"intent": "draft", "tone": "neutral"})
    assert EmittedEvent.from_dict(ev.to_dict()) == ev


def test_deliverable_round_trips_through_sdk() -> None:
    d = Deliverable("design_doc", "document")
    assert Deliverable.from_dict(d.to_dict()) == d


@pytest.mark.parametrize(
    "effect",
    [
        KGEffect.create("node:x", "Project", {"status": "active"}),
        KGEffect.mutate("node:x", "stage", "approved"),
        KGEffect.add_edge("edge:1", "reviews_for", "p1", "p2"),
        KGEffect.milestone("design_signed_off"),
    ],
)
def test_kgeffect_round_trips(effect: KGEffect) -> None:
    assert KGEffect.from_dict(effect.to_dict()) == effect


def test_kgeffect_kind_is_enum_after_round_trip() -> None:
    restored = KGEffect.from_dict(KGEffect.milestone("ship").to_dict())
    assert restored.kind is EffectKind.MILESTONE


def test_condition_expr_round_trips() -> None:
    c = ConditionExpr(node="sku:widget", attr="stock_level", op="lte", value=10)
    assert ConditionExpr.from_dict(c.to_dict()) == c


def test_spread_round_trips() -> None:
    s = Spread(role="reviewers", per_actor="2..5", over="duration", emits="CommentPosted")
    assert Spread.from_dict(s.to_dict()) == s


def test_step_round_trips_with_every_field() -> None:
    step = Step(
        id="review",
        by="reviewers",
        after="draft",
        offset="1d",
        duration="3d",
        slot="30m",
        emits=(EmittedEvent("ReviewOpened"), EmittedEvent("Notified")),
        produces=Deliverable("design_doc", "document"),
        effects=(KGEffect.milestone("reviewed"),),
        repeat=Spread(role="reviewers", per_actor="1..3"),
        when=ConditionExpr(node="study:x", attr="stage", op="eq", value="open"),
        parent_step="draft",
    )
    assert Step.from_dict(step.to_dict()) == step


def test_declares_round_trips() -> None:
    d = Declares(events=("A", "B"), deliverables=("doc",), effects=("mutate:stage",))
    assert Declares.from_dict(d.to_dict()) == d


def test_process_declarative_round_trips() -> None:
    p = Process(
        name="design_review",
        description="run a review",
        roles=(Role(name="lead"),),
        params={"k": "v"},
        steps=(Step(id="draft", by="lead", emits=(EmittedEvent("Drafted"),)),),
        declares=Declares(events=("Drafted",)),
        priority=50,
    )
    assert Process.from_dict(p.to_dict()) == p


def test_process_impl_hatch_round_trips() -> None:
    p = Process(
        name="purchase_order",
        impl="enterprise_sim.processes.purchase_order:PurchaseOrder",
        declares=Declares(events=("PODrafted",), deliverables=("purchase_order",)),
    )
    restored = Process.from_dict(p.to_dict())
    assert restored == p
    assert restored.impl == "enterprise_sim.processes.purchase_order:PurchaseOrder"
    assert restored.steps == ()


def test_activation_round_trips() -> None:
    a = Activation(
        id="raise_po",
        process=Process(name="po", impl="pkg:PO"),
        trigger=OnEvent("NegotiationClosed", where=(Match("project", "eq", "p1"),)),
        bind={"supplier": ("supplier:acme",)},
        anchor="supplier:acme",
        params={"urgency": "high"},
    )
    assert Activation.from_dict(a.to_dict()) == a


# --------------------------------------------------------------------------- #
# The six triggers — each round-trips through the tagged dispatcher.
# --------------------------------------------------------------------------- #


ALL_TRIGGERS: list[Trigger] = [
    OnStart(),
    OnCadence("per_sprint:2w"),
    OnEvent("SprintPlanned", where=(Match("project", "eq", "checkout"),)),
    OnMilestone("release_shipped"),
    OnCondition(ConditionExpr(node="sku:w", attr="stock_level", op="lte", value=10)),
    Probabilistic(rate=0.5, per="week"),
]


@pytest.mark.parametrize("trigger", ALL_TRIGGERS, ids=lambda t: type(t).__name__)
def test_each_trigger_round_trips(trigger: Trigger) -> None:
    assert trigger_from_dict(trigger.to_dict()) == trigger


def test_all_six_triggers_are_distinct_kinds() -> None:
    tags = {t.to_dict()["trigger"] for t in ALL_TRIGGERS}
    assert tags == {
        "on_start",
        "on_cadence",
        "on_event",
        "on_milestone",
        "on_condition",
        "probabilistic",
    }


def test_unknown_trigger_tag_raises() -> None:
    with pytest.raises(ValueError, match="unknown trigger tag"):
        trigger_from_dict({"trigger": "on_eclipse"})


# --------------------------------------------------------------------------- #
# The three reference patterns (§12.3) — express + round-trip (the acceptance).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["build_software", "sell_merchandise", "run_clinical_study"])
def test_reference_playbook_round_trips(name: str) -> None:
    playbook = REFERENCE_PLAYBOOKS[name]()
    assert Playbook.from_dict(playbook.to_dict()) == playbook


@pytest.mark.parametrize("name", ["build_software", "sell_merchandise", "run_clinical_study"])
def test_reference_playbook_is_json_stable(name: str) -> None:
    playbook = REFERENCE_PLAYBOOKS[name]()
    text = json.dumps(playbook.to_dict())
    assert Playbook.from_dict(json.loads(text)) == playbook


def test_registry_matches_named_builders() -> None:
    assert REFERENCE_PLAYBOOKS["build_software"] is build_software
    assert REFERENCE_PLAYBOOKS["sell_merchandise"] is sell_merchandise
    assert REFERENCE_PLAYBOOKS["run_clinical_study"] is run_clinical_study


# --------------------------------------------------------------------------- #
# The patterns actually exercise the format they claim to (§12.3 evidence).
# --------------------------------------------------------------------------- #


def test_build_software_uses_cadence_event_and_milestone() -> None:
    pb = build_software()
    triggers = {type(a.trigger) for a in pb.activations}
    assert {OnCadence, OnEvent, OnMilestone} <= triggers
    # The review step spreads multi-actor comments over a multi-day window.
    review = next(p for a in pb.activations for p in [a.process] if p.name == "design_review")
    review_step = next(s for s in review.steps if s.id == "review")
    assert review_step.repeat is not None
    assert review_step.duration == "3d"


def test_sell_merchandise_uses_condition_event_external_and_impl() -> None:
    pb = sell_merchandise()
    triggers = {type(a.trigger) for a in pb.activations}
    assert {OnStart, OnCondition, OnEvent} <= triggers
    # External supplier party.
    assert any(r.select is not None and r.select.external for r in pb.roles)
    # Stateful lifecycle behind the impl hatch.
    impls = [a.process.impl for a in pb.activations if a.process.impl]
    assert any("PurchaseOrder" in impl for impl in impls)


def test_run_clinical_study_is_a_gated_chain_with_probabilistic_ae() -> None:
    pb = run_clinical_study()
    by_id = {a.id: a for a in pb.activations}
    # The gate chain: each approval event triggers the next stage.
    assert isinstance(by_id["irb_on_protocol"].trigger, OnEvent)
    assert by_id["irb_on_protocol"].trigger.type == "ProtocolApproved"
    assert isinstance(by_id["start_on_irb"].trigger, OnEvent)
    assert by_id["start_on_irb"].trigger.type == "IRBApproved"
    # The adverse-event stream is the seeded probabilistic trigger.
    assert isinstance(by_id["ae_stream"].trigger, Probabilistic)


def test_every_reference_emitted_event_is_declared() -> None:
    """Each step's emitted event types appear in its process's declares block.

    Not the full static linter (a separate bead) — just a sanity check that the
    reference patterns are internally consistent with the contract the engine
    trusts (§12.1 ``declares``).
    """
    for build in REFERENCE_PLAYBOOKS.values():
        pb = build()
        for activation in pb.activations:
            process = activation.process
            if process.impl is not None:
                continue  # impl processes declare without inspectable steps
            declared = set(process.declares.events)
            for step in process.steps:
                for emitted in step.emits:
                    assert emitted.type in declared, (process.name, emitted.type)
                if step.repeat is not None:
                    assert step.repeat.emits in declared, (process.name, step.repeat.emits)
