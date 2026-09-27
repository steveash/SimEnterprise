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
  (:class:`InventoryMonitor`, below).
* ``supplier_negotiation`` — fired **OnCondition** when stock dips to the reorder
  point; negotiates with an **external** supplier and emits ``NegotiationClosed``.
* ``purchase_order`` — an **impl** stateful lifecycle (OnEvent) that drafts →
  approves → sends → receives a PO, replenishing ``stock_level``
  (:class:`PurchaseOrder`, below).

The builder returns a fresh :class:`~enterprise_sim.authoring.sdk.Playbook`; it is
lint-clean (Tier 1), conformance- and P-suite-clean (Tier 2), and covered by
``tests/playbooks/test_sell_merchandise.py`` with a committed golden snapshot.

Importing this template registers the playbook and all five of its processes
(the two ``impl`` processes' stateful logic lives in this module too).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

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
from enterprise_sim.core.registry import PLAYBOOKS, PROCESSES

__all__ = [
    "REORDER_POINT",
    "SKU",
    "SUPPLIER",
    "TRANSITION_EVENTS",
    "InventoryMonitor",
    "POState",
    "PurchaseOrder",
    "sell_merchandise",
]

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
        impl="enterprise_sim_templates.sell_merchandise:InventoryMonitor",
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
        impl="enterprise_sim_templates.sell_merchandise:PurchaseOrder",
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


# --------------------------------------------------------------------------- #
# ``inventory_monitor`` — the ``impl`` escape hatch.
# --------------------------------------------------------------------------- #
#
# ``impl`` escape hatch: a stateful inventory monitor (ARCHITECTURE §12.1, D25).
#
# The declarative :class:`~enterprise_sim.authoring.sdk.Step` surface can express a
# *timed sequence* of work, but not a **stateful watcher** that ticks the simulated
# world forward day by day, draws demand, decrements stock, and fires only when a
# threshold is crossed. That is exactly the kind of logic the §12.1 ``impl`` escape
# hatch exists for, and the ``sell_merchandise`` playbook's ``inventory_monitor``
# process binds this class via
# ``impl="enterprise_sim_templates.sell_merchandise:InventoryMonitor"``.
#
# **Contract the engine trusts.** The process's ``declares`` block (``events=("LowStock",)``,
# ``effects=("mutate:stock_level",)``) is what the engine and the linter read; this
# class is the realisation that conformance **I5** checks the run against dynamically.
# Because there is no ``impl`` runner in the engine yet (see the test-kit scope note),
# this class is unit-tested directly rather than driven by ``run_process`` — but it is
# written to the same deterministic contract so it drops straight in when the runner
# lands.
#
# **Determinism (D26 / lint AST rule / conformance I8).** Nothing here reads the
# wall clock or the module-level :mod:`random` conveniences. All variability comes
# from a caller-seeded :class:`random.Random` instance, so a given ``(seed, config,
# window)`` always yields byte-identical output — the property I6/I8 enforce.


@dataclass(frozen=True, slots=True)
class StockReading:
    """One day's view of a SKU's stock, and whether it tripped the reorder point.

    Attributes:
        day: The reading's day index from the monitoring window start (0-based).
        timestamp: The reading's wall-time within the window (window start + ``day``).
        sku: The SKU node id the reading is about (e.g. ``"sku:widget"``).
        units_sold: Units the seeded demand draw removed that day (``>= 0``).
        stock_level: Remaining on-hand stock after the day's demand (clamped at 0).
        reorder_point: The threshold below which a restock is needed.
        low: ``True`` iff ``stock_level <= reorder_point`` — i.e. ``LowStock`` fires.
    """

    day: int
    timestamp: datetime
    sku: str
    units_sold: int
    stock_level: int
    reorder_point: int
    low: bool


@dataclass(slots=True)
class InventoryMonitor:
    """A stateful watcher that emits ``LowStock`` when a SKU dips to its reorder point.

    The ``sell_merchandise`` ``inventory_monitor`` process's ``impl``. Seeded from
    the scenario root seed, it walks the monitoring window one working concept-day at
    a time, draws that day's demand from a seeded distribution, decrements on-hand
    stock, and — the first time stock falls to or below the reorder point — yields a
    low reading whose KG effect mutates ``stock_level`` and whose business event is
    ``LowStock``. It fires **once per dip** (it goes quiet until a restock lifts stock
    back above the reorder point), so a flat low shelf does not spam the event log.

    Attributes:
        sku: The SKU node id this monitor watches.
        initial_stock: On-hand units at the window start.
        reorder_point: Stock level at/below which ``LowStock`` fires.
        mean_daily_demand: Mean of the seeded daily-demand draw.
        seed: Sub-stream seed; combined with the SKU id for an independent RNG.
    """

    sku: str = "sku:widget"
    initial_stock: int = 60
    reorder_point: int = 10
    mean_daily_demand: float = 6.0
    seed: int = 1
    _rng: random.Random = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # A per-SKU sub-stream so two monitored SKUs in one scenario stay independent
        # yet reproducible. ``hash`` is salted per-process, so derive a stable int
        # from the SKU id ourselves rather than relying on ``hash(self.sku)``.
        salt = sum(ord(ch) for ch in self.sku)
        self._rng = random.Random(self.seed * 1_000 + salt)

    def _demand(self) -> int:
        """Draw one day's demand from the seeded distribution (never negative)."""
        # ``triangular`` over [0, 2*mean] centres on the mean with bounded tails — a
        # plausible daily-sales shape, and deterministic because ``_rng`` is seeded.
        drawn = self._rng.triangular(0.0, 2.0 * self.mean_daily_demand, self.mean_daily_demand)
        return max(0, round(drawn))

    def readings(self, start: datetime, days: int) -> Iterator[StockReading]:
        """Yield a :class:`StockReading` for each of ``days`` days from ``start``.

        Pure generator over the seeded demand process — the caller decides which
        readings become events. ``stock_level`` is clamped at zero (you cannot sell
        what you do not have); ``low`` marks readings at or below the reorder point.
        """
        stock = self.initial_stock
        for day in range(days):
            sold = min(stock, self._demand())
            stock -= sold
            yield StockReading(
                day=day,
                timestamp=start + timedelta(days=day),
                sku=self.sku,
                units_sold=sold,
                stock_level=stock,
                reorder_point=self.reorder_point,
                low=stock <= self.reorder_point,
            )

    def low_stock_events(self, start: datetime, days: int) -> list[StockReading]:
        """The readings that should emit ``LowStock`` — one per distinct dip.

        Edge-triggered, not level-triggered: a ``LowStock`` is reported the day stock
        first crosses to/below the reorder point, then suppressed until a reading
        recovers above it (a restock), so a long low shelf yields a single event
        rather than one per day.
        """
        events: list[StockReading] = []
        armed = True
        for reading in self.readings(start, days):
            if reading.low and armed:
                events.append(reading)
                armed = False
            elif not reading.low:
                armed = True
        return events


# --------------------------------------------------------------------------- #
# ``purchase_order`` — the ``impl`` escape hatch.
# --------------------------------------------------------------------------- #
#
# ``impl`` escape hatch: a stateful purchase-order lifecycle (ARCHITECTURE §12.1, D25).
#
# A purchase order is not a fixed sequence of timed steps — it is a **state machine**
# with gates: a draft must be *approved* before it can be *sent*, and a sent PO is only
# *received* after a supplier-specific lead time, at which point stock is replenished.
# That guarded, stateful shape is what the §12.1 ``impl`` escape hatch is for, and the
# ``sell_merchandise`` playbook's ``purchase_order`` process binds this class via
# ``impl="enterprise_sim_templates.sell_merchandise:PurchaseOrder"``.
#
# **Contract the engine trusts.** The process ``declares`` its events
# (``PODrafted`` → ``POApproved`` → ``POSent`` → ``POReceived``), its
# ``purchase_order`` deliverable, and its ``mutate:stock_level`` effect; that block is
# what the engine, the linter, and playbook invariant **P4** read. This class is the
# realisation conformance **I5** would check a real run against. As with
# :mod:`enterprise_sim.processes.inventory`, there is no ``impl`` runner in the engine
# yet, so it is unit-tested directly — but written to the same deterministic contract.
#
# **Determinism (D26 / lint AST rule / conformance I8).** No wall-clock reads, no
# module-level :mod:`random`. Lead times are fixed offsets and any jitter comes from a
# caller-seeded :class:`random.Random`, so a given ``(seed, order, start)`` always
# produces the identical transition stream.


class POState(StrEnum):
    """The ordered states a purchase order moves through (D25 stateful lifecycle)."""

    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    RECEIVED = "received"


#: The event each state transition emits, in lifecycle order. The keys match the
#: process's ``declares.events`` exactly (the contract conformance I5 checks).
TRANSITION_EVENTS: dict[POState, str] = {
    POState.DRAFT: "PODrafted",
    POState.APPROVED: "POApproved",
    POState.SENT: "POSent",
    POState.RECEIVED: "POReceived",
}


@dataclass(frozen=True, slots=True)
class POTransition:
    """One step of the PO lifecycle: a state reached, its event, and its timestamp.

    Attributes:
        state: The :class:`POState` now entered.
        event_type: The business event this transition emits (see
            :data:`TRANSITION_EVENTS`).
        timestamp: When the transition lands (start + accumulated lead time).
        stock_delta: Units added to ``stock_level`` by this transition — non-zero
            only on ``RECEIVED`` (the replenishment), zero otherwise.
    """

    state: POState
    event_type: str
    timestamp: datetime
    stock_delta: int = 0


@dataclass(slots=True)
class PurchaseOrder:
    """A stateful PO that drafts, gets approved, is sent, and is received (restocking).

    The ``sell_merchandise`` ``purchase_order`` process's ``impl``. Constructed when
    a ``NegotiationClosed`` event triggers the activation, it plays its lifecycle out
    deterministically: each transition is offset from the previous by a fixed,
    working-day lead time (with optional small seeded jitter on the supplier delivery
    leg), emits its declared event, and the terminal ``POReceived`` mutates the SKU's
    ``stock_level`` by :attr:`quantity` — closing the low-stock cascade the playbook
    models.

    Attributes:
        sku: The SKU node id this PO replenishes.
        supplier: The external supplier node id the PO is placed with.
        quantity: Units ordered (the restock applied on receipt).
        approval_days: Working days from draft to approval.
        send_days: Working days from approval to dispatch.
        lead_days: Nominal working days from dispatch to receipt (delivery lead time).
        seed: Sub-stream seed for the bounded delivery-jitter draw.
    """

    sku: str = "sku:widget"
    supplier: str = "supplier:acme"
    quantity: int = 100
    approval_days: int = 1
    send_days: int = 1
    lead_days: int = 5
    seed: int = 1
    _rng: random.Random = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Stable per-order sub-stream: salt the seed with the supplier id so two
        # concurrent POs to different suppliers jitter independently yet reproducibly.
        salt = sum(ord(ch) for ch in self.supplier)
        self._rng = random.Random(self.seed * 7_919 + salt)

    def _delivery_days(self) -> int:
        """Delivery lead time in days: the nominal lead ± a bounded seeded jitter."""
        jitter = self._rng.randint(-1, 1)
        return max(1, self.lead_days + jitter)

    def lifecycle(self, start: datetime) -> Iterator[POTransition]:
        """Yield the four lifecycle transitions in order from ``start``.

        Timestamps accumulate the per-leg lead times; only the final ``POReceived``
        carries a positive :attr:`quantity` ``stock_delta`` (the replenishment the
        ``mutate:stock_level`` effect applies).
        """
        when = start
        yield POTransition(POState.DRAFT, TRANSITION_EVENTS[POState.DRAFT], when)

        when = when + timedelta(days=self.approval_days)
        yield POTransition(POState.APPROVED, TRANSITION_EVENTS[POState.APPROVED], when)

        when = when + timedelta(days=self.send_days)
        yield POTransition(POState.SENT, TRANSITION_EVENTS[POState.SENT], when)

        when = when + timedelta(days=self._delivery_days())
        yield POTransition(
            POState.RECEIVED,
            TRANSITION_EVENTS[POState.RECEIVED],
            when,
            stock_delta=self.quantity,
        )

    def restocked_level(self, current_stock: int, start: datetime) -> int:
        """The ``stock_level`` after this PO is received — ``current_stock + quantity``.

        Convenience for the cascade's terminal KG effect: the level the
        ``mutate:stock_level`` lands once ``POReceived`` fires. ``start`` is accepted
        for symmetry with :meth:`lifecycle` (and to keep the run deterministic).
        """
        for transition in self.lifecycle(start):
            current_stock += transition.stock_delta
        return current_stock


# --------------------------------------------------------------------------- #
# Registration: the playbook and all five of its processes.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _PlaybookPlugin:
    """A registrable playbook (the registry protocol + a fresh-tree factory)."""

    name: str
    vertical: str
    deliverables: Sequence[str]
    build: Callable[[], Playbook]


@dataclass(slots=True)
class _ProcessPlugin:
    """A registrable process: declared events/deliverables + a fresh-process factory."""

    name: str
    emits: Sequence[str]
    requests: Sequence[str]
    build: Callable[[], Process]


def _process_factory(name: str) -> Callable[[], Process]:
    """A zero-arg factory for one of the playbook's processes (built fresh each call)."""

    def build() -> Process:
        return next(a.process for a in sell_merchandise().activations if a.process.name == name)

    return build


def _register() -> _PlaybookPlugin:
    """Register the playbook + its processes into the process-wide catalogs (once, on import)."""
    playbook = sell_merchandise()
    for activation in playbook.activations:
        process = activation.process
        if process.name in PROCESSES:
            continue
        PROCESSES.register(
            _ProcessPlugin(
                name=process.name,
                emits=process.declares.events,
                requests=process.declares.deliverables,
                build=_process_factory(process.name),
            )
        )
    plugin = _PlaybookPlugin(
        name=playbook.name,
        vertical=playbook.vertical,
        deliverables=playbook.deliverable_expectations,
        build=sell_merchandise,
    )
    PLAYBOOKS.register(plugin)
    return plugin


#: The registered playbook plugin (registration fires on import).
SELL_MERCHANDISE = _register()
