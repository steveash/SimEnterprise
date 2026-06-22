"""The three cross-vertical reference playbooks, expressed through the SDK (§12.3).

These are the worked patterns that prove the authoring format needs **no new
engine primitives** across very different domains, and become the
``author-playbook`` skill's pattern library (§14):

* :func:`build_software` (technology) — ``OnCadence`` sprints + ``OnEvent`` design
  reviews + an ``OnMilestone`` ship. Mostly declarative.
* :func:`sell_merchandise` (retail) — an ``OnCondition`` / ``OnEvent`` cascade
  (low-stock → supplier negotiation → PO), an **external** ``supplier``, a
  **stateful** PO lifecycle behind the ``impl`` hatch, and a monitor process.
* :func:`run_clinical_study` (pharma) — a **gated event-chain**
  (``ProtocolApproved`` → ``IRBApproved`` → …), **external** regulators
  (CRO/IRB), and an urgent ``Probabilistic`` adverse-event report with an SLA
  sign-off chain.

Together they exercise all six triggers, the ``impl`` escape hatch, external
selectors, multi-actor comment spread, and KG effects/milestones — and each one
round-trips through :meth:`~enterprise_sim.authoring.sdk.Playbook.to_dict` /
``from_dict`` (the acceptance check). Each builder returns a fresh
:class:`~enterprise_sim.authoring.sdk.Playbook`.
"""

from __future__ import annotations

from enterprise_sim.authoring.sdk import (
    Activation,
    ConditionExpr,
    Declares,
    Deliverable,
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
)

__all__ = ["REFERENCE_PLAYBOOKS", "build_software", "run_clinical_study", "sell_merchandise"]


# --------------------------------------------------------------------------- #
# 1. build_software (technology) — cadence sprints, event reviews, ship milestone.
# --------------------------------------------------------------------------- #


def build_software() -> Playbook:
    """The technology reference: cadence sprints, design reviews, a ship milestone.

    A ``sprint_planning`` process fires every two weeks (``OnCadence``); each
    sprint a ``design_review`` is triggered ``OnEvent`` off the planning event and
    runs a multi-day, multi-reviewer comment window (``Spread``); shipping is an
    ``OnMilestone`` retrospective. Mostly declarative — no ``impl`` needed.
    """
    eng_lead = Role(
        name="lead",
        select=Selector(type="Person", where=(Match("role", "eq", "eng_lead"),), count=1),
        description="The engineering lead who plans and ships.",
    )
    reviewers = Role(
        name="reviewers",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", "engineering"),),
            rank_by=("affinity", "inverse_load", "expertise"),
            count="2..3",
        ),
        description="Peer reviewers drawn by affinity + load balance.",
    )

    sprint_planning = Process(
        name="sprint_planning",
        description="Plan the next sprint and open it for design review.",
        roles=(eng_lead,),
        steps=(
            Step(
                id="plan",
                by="lead",
                at="day 0",
                duration="1d",
                emits=(EmittedEvent("SprintPlanned", payload={"intent": "scope the sprint"}),),
                produces=Deliverable("sprint_plan", "document"),
            ),
        ),
        declares=Declares(events=("SprintPlanned",), deliverables=("sprint_plan",)),
    )

    design_review = Process(
        name="design_review",
        description="Draft a design doc and run a multi-day review thread.",
        roles=(eng_lead, reviewers),
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

    ship_retro = Process(
        name="ship_retro",
        description="Write the release notes and retro once the release ships.",
        roles=(eng_lead,),
        steps=(
            Step(
                id="retro",
                by="lead",
                at="day 0",
                duration="1d",
                emits=(EmittedEvent("Shipped"),),
                produces=Deliverable("release_notes", "document"),
            ),
        ),
        declares=Declares(events=("Shipped",), deliverables=("release_notes",)),
    )

    return Playbook(
        name="build_software",
        vertical="technology",
        goal_template="Ship {project} over {n_sprints} two-week sprints.",
        roles=(eng_lead, reviewers),
        activations=(
            Activation(
                id="plan_each_sprint",
                process=sprint_planning,
                trigger=OnCadence("per_sprint:2w"),
                anchor="project:checkout",
            ),
            Activation(
                id="review_on_plan",
                process=design_review,
                trigger=OnEvent("SprintPlanned"),
                anchor="project:checkout",
            ),
            Activation(
                id="retro_on_ship",
                process=ship_retro,
                trigger=OnMilestone("release_shipped"),
                anchor="project:checkout",
            ),
        ),
        deliverable_expectations=("sprint_plan", "design_doc", "release_notes"),
    )


# --------------------------------------------------------------------------- #
# 2. sell_merchandise (retail) — condition/event cascade, external supplier, impl PO.
# --------------------------------------------------------------------------- #


def sell_merchandise() -> Playbook:
    """The retail reference: a low-stock cascade with an external supplier + impl PO.

    An ``impl``-backed ``inventory_monitor`` watches stock and emits ``LowStock``;
    an ``OnCondition`` on the stock level *also* opens a supplier negotiation
    (showing the condition path); the negotiation's ``NegotiationClosed`` event
    triggers (``OnEvent``) a **stateful** purchase-order process whose lifecycle
    lives behind the ``impl`` hatch. The supplier is bound by an **external**
    selector (§12.3).
    """
    buyer = Role(
        name="buyer",
        select=Selector(type="Person", where=(Match("role", "eq", "buyer"),), count=1),
        description="The merchandising buyer who negotiates and raises POs.",
    )
    supplier = Role(
        name="supplier",
        select=Selector(type="Supplier", external=True, count=1),
        description="External supplier party materialised outside the org.",
    )

    inventory_monitor = Process(
        name="inventory_monitor",
        description="Stateful watcher that emits LowStock when a SKU dips below reorder point.",
        roles=(buyer,),
        impl="enterprise_sim.processes.inventory:InventoryMonitor",
        declares=Declares(events=("LowStock",), effects=("mutate:stock_level",)),
    )

    negotiate = Process(
        name="supplier_negotiation",
        description="Negotiate price/terms with the external supplier over email.",
        roles=(buyer, supplier),
        steps=(
            Step(
                id="open",
                by="buyer",
                at="day 0",
                duration="2d",
                emits=(EmittedEvent("NegotiationOpened", payload={"intent": "restock"}),),
                produces=Deliverable("rfq", "email"),
            ),
            Step(
                id="close",
                by="buyer",
                after="open",
                emits=(EmittedEvent("NegotiationClosed"),),
                effects=(KGEffect.mutate("supplier:acme", "terms", "agreed"),),
                parent_step="open",
            ),
        ),
        declares=Declares(
            events=("NegotiationOpened", "NegotiationClosed"),
            deliverables=("rfq",),
            effects=("mutate:terms",),
        ),
    )

    purchase_order = Process(
        name="purchase_order",
        description="Stateful PO lifecycle (draft → approved → sent → received).",
        roles=(buyer, supplier),
        impl="enterprise_sim.processes.purchase_order:PurchaseOrder",
        declares=Declares(
            events=("PODrafted", "POApproved", "POSent", "POReceived"),
            deliverables=("purchase_order",),
            effects=("mutate:stock_level",),
        ),
    )

    low_stock = ConditionExpr(node="sku:widget", attr="stock_level", op="lte", value=10)

    return Playbook(
        name="sell_merchandise",
        vertical="retail",
        goal_template="Keep {sku} in stock by reordering from {supplier} when low.",
        roles=(buyer, supplier),
        activations=(
            Activation(
                id="monitor_from_start",
                process=inventory_monitor,
                trigger=OnStart(),
                anchor="sku:widget",
            ),
            Activation(
                id="negotiate_on_low_stock",
                process=negotiate,
                trigger=OnCondition(low_stock),
                anchor="supplier:acme",
                bind={"supplier": ("supplier:acme",)},
            ),
            Activation(
                id="raise_po_on_close",
                process=purchase_order,
                trigger=OnEvent("NegotiationClosed"),
                anchor="supplier:acme",
                bind={"supplier": ("supplier:acme",)},
            ),
        ),
        deliverable_expectations=("rfq", "purchase_order"),
    )


# --------------------------------------------------------------------------- #
# 3. run_clinical_study (pharma) — gated event-chain, external regulators, prob AE.
# --------------------------------------------------------------------------- #


def run_clinical_study() -> Playbook:
    """The pharma reference: a gated approval chain + a probabilistic adverse-event SLA.

    A linear **gate chain** wires each approval to the next via ``OnEvent``
    (``ProtocolApproved`` → ``IRBApproved`` → ``StudyStarted``), with the IRB and
    CRO bound by **external** selectors. Adverse events arrive as a seeded
    ``Probabilistic`` stream; each opens an urgent report with an SLA-bounded
    sign-off chain (a multi-step process emitting a ``SafetySignedOff`` milestone).
    """
    investigator = Role(
        name="investigator",
        select=Selector(type="Person", where=(Match("role", "eq", "investigator"),), count=1),
        description="Principal investigator who authors the protocol.",
    )
    irb = Role(
        name="irb",
        select=Selector(type="IRB", external=True, count=1),
        description="External institutional review board.",
    )
    safety = Role(
        name="safety",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", "safety"),),
            expertise=("pharmacovigilance",),
            rank_by=("expertise", "inverse_load"),
            count="1..2",
        ),
        description="Safety reviewers who sign off adverse-event reports.",
    )

    author_protocol = Process(
        name="author_protocol",
        description="Draft the study protocol and submit it for approval.",
        roles=(investigator,),
        steps=(
            Step(
                id="draft",
                by="investigator",
                at="day 0",
                duration="5d",
                emits=(EmittedEvent("ProtocolDrafted"),),
                produces=Deliverable("protocol", "document"),
            ),
            Step(
                id="submit",
                by="investigator",
                after="draft",
                emits=(EmittedEvent("ProtocolApproved"),),
                effects=(KGEffect.mutate("study:trial7", "stage", "protocol_approved"),),
                parent_step="draft",
            ),
        ),
        declares=Declares(
            events=("ProtocolDrafted", "ProtocolApproved"),
            deliverables=("protocol",),
            effects=("mutate:stage",),
        ),
    )

    irb_review = Process(
        name="irb_review",
        description="External IRB reviews the approved protocol and clears the study.",
        roles=(investigator, irb),
        steps=(
            Step(
                id="review",
                by="irb",
                at="day 0",
                duration="10d",
                emits=(EmittedEvent("IRBApproved"),),
                produces=Deliverable("irb_approval", "document"),
                effects=(KGEffect.mutate("study:trial7", "stage", "irb_approved"),),
            ),
        ),
        declares=Declares(
            events=("IRBApproved",),
            deliverables=("irb_approval",),
            effects=("mutate:stage",),
        ),
    )

    start_study = Process(
        name="start_study",
        description="Open enrollment once IRB approval lands.",
        roles=(investigator,),
        steps=(
            Step(
                id="start",
                by="investigator",
                at="day 0",
                emits=(EmittedEvent("StudyStarted"),),
                effects=(KGEffect.milestone("enrollment_open"),),
            ),
        ),
        declares=Declares(events=("StudyStarted",), effects=("milestone:enrollment_open",)),
    )

    adverse_event = Process(
        name="adverse_event_report",
        description="Urgent AE report with an SLA-bounded safety sign-off chain.",
        roles=(investigator, safety),
        steps=(
            Step(
                id="report",
                by="investigator",
                at="day 0",
                emits=(EmittedEvent("AdverseEventReported", payload={"severity": "serious"}),),
                produces=Deliverable("ae_report", "document"),
            ),
            Step(
                id="triage",
                by="safety",
                after="report",
                offset="1d",
                duration="1d",
                emits=(EmittedEvent("SafetyReviewed"),),
                repeat=Spread(role="safety", per_actor="1..2", emits="SafetyComment"),
                parent_step="report",
            ),
            Step(
                id="signoff",
                by="safety",
                after="triage",
                emits=(EmittedEvent("SafetySignedOff"),),
                effects=(KGEffect.milestone("ae_signed_off"),),
                parent_step="triage",
            ),
        ),
        declares=Declares(
            events=("AdverseEventReported", "SafetyReviewed", "SafetyComment", "SafetySignedOff"),
            deliverables=("ae_report",),
            effects=("milestone:ae_signed_off",),
        ),
    )

    return Playbook(
        name="run_clinical_study",
        vertical="pharma",
        goal_template="Run {study} through approval gates and monitor safety.",
        roles=(investigator, irb, safety),
        activations=(
            Activation(
                id="author_on_start",
                process=author_protocol,
                trigger=OnStart(),
                anchor="study:trial7",
            ),
            Activation(
                id="irb_on_protocol",
                process=irb_review,
                trigger=OnEvent("ProtocolApproved"),
                anchor="study:trial7",
                bind={"irb": ("irb:central",)},
            ),
            Activation(
                id="start_on_irb",
                process=start_study,
                trigger=OnEvent("IRBApproved"),
                anchor="study:trial7",
            ),
            Activation(
                id="ae_stream",
                process=adverse_event,
                trigger=Probabilistic(rate=0.5, per="week"),
                anchor="study:trial7",
            ),
        ),
        deliverable_expectations=("protocol", "irb_approval", "ae_report"),
    )


#: The three reference playbooks, keyed by name (the skill's pattern library).
REFERENCE_PLAYBOOKS = {
    "build_software": build_software,
    "sell_merchandise": sell_merchandise,
    "run_clinical_study": run_clinical_study,
}
