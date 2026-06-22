"""The ``sell_merchandise`` retail playbook (ARCHITECTURE §12.3, D25).

The full retail vertical, authored end-to-end through the declarative SDK with the
engine core untouched. It is the worked example for the ``author-playbook`` skill's
"condition cascade + external counterparty + stateful lifecycle + ``impl``" recipe,
and it exercises, between five processes, **OnCadence + OnEvent + OnCondition +
OnStart** triggers, an **external** supplier, multi-actor comment **spread**, and the
``impl`` escape hatch for two kinds of logic the declarative steps cannot express
(a stateful watcher and a guarded lifecycle).

The triggering graph::

    OnCadence(weekly:FRI) ─▶ weekly_sales_ops_review ──SalesReviewCompiled──▶ demand_forecast
                                                                                    │
                                                                          (sets reorder_point)
    OnStart() ───────────▶ inventory_monitor  (impl: watches stock, emits LowStock,
                                                mutates stock_level)
                                  │
                          stock_level ≤ 10
                                  ▼
    OnCondition ─────────▶ supplier_negotiation ──NegotiationClosed──▶ purchase_order
                           (external supplier)                         (impl: draft→approved
                                                                        →sent→received,
                                                                        mutates stock_level)

Five processes:

* :func:`weekly_sales_ops_review` — a recurring **OnCadence** review with a
  multi-actor comment **spread**; its closing ``SalesReviewCompiled`` event feeds the
  forecast.
* ``demand_forecast`` — fired **OnEvent** off the weekly review; publishes a forecast
  and mutates the SKU's ``reorder_point``.
* ``inventory_monitor`` — an **impl** stateful watcher (OnStart) that emits
  ``LowStock`` and mutates ``stock_level``
  (:class:`enterprise_sim.processes.inventory.InventoryMonitor`).
* ``supplier_negotiation`` — fired **OnCondition** when stock dips to the reorder
  point; negotiates with an **external** supplier and emits ``NegotiationClosed``.
* ``purchase_order`` — an **impl** stateful lifecycle (OnEvent) that drafts →
  approves → sends → receives a PO, replenishing ``stock_level``
  (:class:`enterprise_sim.processes.purchase_order.PurchaseOrder`).

The builder returns a fresh :class:`~enterprise_sim.authoring.sdk.Playbook`; it is
lint-clean (Tier 1), conformance- and P-suite-clean (Tier 2), and covered by
``tests/playbooks/test_sell_merchandise.py`` with a committed golden snapshot.
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
    OnStart,
    Playbook,
    Process,
    Role,
    Selector,
    Spread,
    Step,
)

__all__ = ["sell_merchandise"]

#: The SKU the scenario keeps in stock — the focal anchor of the whole cascade.
SKU = "sku:widget"
#: The external supplier the buyer negotiates and places orders with.
SUPPLIER = "supplier:acme"
#: Reorder threshold: stock at/below this trips the supplier negotiation.
REORDER_POINT = 10


def sell_merchandise() -> Playbook:
    """Build the full ``sell_merchandise`` retail playbook (§12.3).

    Returns:
        A fresh :class:`~enterprise_sim.authoring.sdk.Playbook` wiring the five
        retail processes into the low-stock-to-restock triggering graph described in
        the module docstring.
    """
    # --- Roles ----------------------------------------------------------- #
    sales_lead = Role(
        name="sales_lead",
        select=Selector(type="Person", where=(Match("role", "eq", "sales_lead"),), count=1),
        description="The sales lead who chairs the weekly ops review.",
    )
    sales_team = Role(
        name="sales_team",
        select=Selector(
            type="Person",
            where=(Match("team", "eq", "sales"),),
            rank_by=("affinity", "inverse_load"),
            count="2..3",
        ),
        description="Sales reps who weigh in on the weekly review thread.",
    )
    planner = Role(
        name="planner",
        select=Selector(
            type="Person",
            where=(Match("role", "eq", "demand_planner"),),
            expertise=("forecasting",),
            rank_by=("expertise", "inverse_load"),
            count=1,
        ),
        description="Demand planner who turns the review into a forecast.",
    )
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

    # --- Process 1: weekly sales-ops review (OnCadence + Spread) ---------- #
    weekly_review = Process(
        name="weekly_sales_ops_review",
        description="Recurring review of the week's sales, with a team comment thread.",
        roles=(sales_lead, sales_team),
        steps=(
            Step(
                id="compile",
                by="sales_lead",
                at="day 0",
                duration="1d",
                emits=(EmittedEvent("SalesReviewOpened", payload={"intent": "review the week"}),),
                produces=Deliverable("sales_review", "document"),
            ),
            Step(
                id="discuss",
                by="sales_team",
                after="compile",
                duration="2d",
                emits=(EmittedEvent("SalesReviewDiscussed"),),
                repeat=Spread(role="sales_team", per_actor="1..3", emits="SalesComment"),
                parent_step="compile",
            ),
            Step(
                id="close",
                by="sales_lead",
                after="discuss",
                emits=(EmittedEvent("SalesReviewCompiled"),),
                parent_step="discuss",
            ),
        ),
        declares=Declares(
            events=(
                "SalesReviewOpened",
                "SalesReviewDiscussed",
                "SalesComment",
                "SalesReviewCompiled",
            ),
            deliverables=("sales_review",),
        ),
    )

    # --- Process 2: demand forecast (OnEvent off the review) -------------- #
    demand_forecast = Process(
        name="demand_forecast",
        description="Model next period's demand and reset the SKU's reorder point.",
        roles=(planner,),
        steps=(
            Step(
                id="model",
                by="planner",
                at="day 0",
                duration="2d",
                emits=(EmittedEvent("ForecastDrafted"),),
                produces=Deliverable("demand_forecast", "document"),
            ),
            Step(
                id="publish",
                by="planner",
                after="model",
                emits=(EmittedEvent("ForecastPublished"),),
                effects=(KGEffect.mutate(SKU, "reorder_point", REORDER_POINT),),
                parent_step="model",
            ),
        ),
        declares=Declares(
            events=("ForecastDrafted", "ForecastPublished"),
            deliverables=("demand_forecast",),
            effects=("mutate:reorder_point",),
        ),
    )

    # --- Process 3: inventory monitor (impl, OnStart) -------------------- #
    inventory_monitor = Process(
        name="inventory_monitor",
        description="Stateful watcher that emits LowStock when the SKU dips below reorder.",
        roles=(buyer,),
        impl="enterprise_sim.processes.inventory:InventoryMonitor",
        declares=Declares(events=("LowStock",), effects=("mutate:stock_level",)),
    )

    # --- Process 4: supplier negotiation (OnCondition, external) --------- #
    supplier_negotiation = Process(
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
                effects=(KGEffect.mutate(SUPPLIER, "terms", "agreed"),),
                parent_step="open",
            ),
        ),
        declares=Declares(
            events=("NegotiationOpened", "NegotiationClosed"),
            deliverables=("rfq",),
            effects=("mutate:terms",),
        ),
    )

    # --- Process 5: purchase order (impl stateful lifecycle, OnEvent) ---- #
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

    low_stock = ConditionExpr(node=SKU, attr="stock_level", op="lte", value=REORDER_POINT)

    return Playbook(
        name="sell_merchandise",
        vertical="retail",
        goal_template="Keep {sku} in stock by reordering from {supplier} when low.",
        roles=(sales_lead, sales_team, planner, buyer, supplier),
        activations=(
            Activation(
                id="weekly_review",
                process=weekly_review,
                trigger=OnCadence("weekly:FRI"),
                anchor=SKU,
            ),
            Activation(
                id="forecast_on_review",
                process=demand_forecast,
                trigger=OnEvent("SalesReviewCompiled"),
                anchor=SKU,
            ),
            Activation(
                id="monitor_from_start",
                process=inventory_monitor,
                trigger=OnStart(),
                anchor=SKU,
            ),
            Activation(
                id="negotiate_on_low_stock",
                process=supplier_negotiation,
                trigger=OnCondition(low_stock),
                anchor=SUPPLIER,
                bind={"supplier": (SUPPLIER,)},
            ),
            Activation(
                id="raise_po_on_close",
                process=purchase_order,
                trigger=OnEvent("NegotiationClosed"),
                anchor=SUPPLIER,
                bind={"supplier": (SUPPLIER,)},
            ),
        ),
        deliverable_expectations=("sales_review", "demand_forecast", "rfq", "purchase_order"),
    )
