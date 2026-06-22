"""``impl`` escape hatch: a stateful purchase-order lifecycle (ARCHITECTURE §12.1, D25).

A purchase order is not a fixed sequence of timed steps — it is a **state machine**
with gates: a draft must be *approved* before it can be *sent*, and a sent PO is only
*received* after a supplier-specific lead time, at which point stock is replenished.
That guarded, stateful shape is what the §12.1 ``impl`` escape hatch is for, and the
``sell_merchandise`` playbook's ``purchase_order`` process binds this class via
``impl="enterprise_sim.processes.purchase_order:PurchaseOrder"``.

**Contract the engine trusts.** The process ``declares`` its events
(``PODrafted`` → ``POApproved`` → ``POSent`` → ``POReceived``), its
``purchase_order`` deliverable, and its ``mutate:stock_level`` effect; that block is
what the engine, the linter, and playbook invariant **P4** read. This class is the
realisation conformance **I5** would check a real run against. As with
:mod:`enterprise_sim.processes.inventory`, there is no ``impl`` runner in the engine
yet, so it is unit-tested directly — but written to the same deterministic contract.

**Determinism (D26 / lint AST rule / conformance I8).** No wall-clock reads, no
module-level :mod:`random`. Lead times are fixed offsets and any jitter comes from a
caller-seeded :class:`random.Random`, so a given ``(seed, order, start)`` always
produces the identical transition stream.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


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
