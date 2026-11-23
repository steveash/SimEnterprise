"""``ap_month_end``: the ``ap`` book with every accrual trap at every month end, for both entities.

The ``ap`` scenario plants each trap once, at the first month end with room after it.
A test-set family that draws a new instance per month end needs the traps repeated:
here every month end of the window (at least 12 days after its start, so the goods
can be received inside it) gets, for each of the two entities, four purchase orders
of its own:

* **cross_month:** received in the last days before the month end, invoiced in full a
  few days after it (or not at all, when that falls past the window), so it is accrued.
* **partial:** received before the month end, half invoiced before it and the rest
  after it, so the remainder is accrued.
* **over_invoiced:** received and invoiced before the month end, the invoice above the
  receipt. Nothing is accrued for it; netting invoices against receipts across POs
  would hide another PO's accrual.
* **wrong_entity:** received before the month end and invoiced in full before it, but
  booked to the other entity. Matched by ``po_id`` alone the accrual looks cleared;
  scoped by entity it is still open (and stays open).

The base rows are ``ap``'s (the same draws, so the same orders, receipts and invoices);
the trap orders come after them, from a stream of their own, with the next PO and
invoice numbers. The tables are ``ap``'s three.

Gold ``truth.json``: the entities, the open accruals at each month end by entity, the
over-invoiced POs at each month end (PO, entity, excess), and ``traps``: one entry per
planted order with its kind, month end, entity and the invoices that make it.
Questions: per month end the accruals and the over-invoiced POs; once, the invoices
booked to another entity than their PO's.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from enterprise_sim.core.world import World
from enterprise_sim.data_products.scripted import (
    SCRIPTED_SCENARIOS,
    ScriptedData,
    ScriptedQuestion,
)
from enterprise_sim.data_products.scripted_scenarios.ap import (
    VENDORS,
    Book,
    accrual_questions,
    accruals,
    base_book,
    billed_by,
    cents,
    month_ends_between,
    tables,
)

#: The trap kinds planted at every month end for each entity, in planting order.
KINDS = ("cross_month", "partial", "over_invoiced", "wrong_entity")
#: How far before the month end each kind's goods are received (days).
_RECEIVED_BEFORE = {"cross_month": 2, "partial": 8, "over_invoiced": 10, "wrong_entity": 5}


def _plant(
    book: Book,
    rng: random.Random,
    *,
    kind: str,
    month_end: date,
    entity: str,
    number: int,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Add one trap order of ``kind`` for ``entity`` at ``month_end``; return its gold entry."""
    received = month_end - timedelta(days=_RECEIVED_BEFORE[kind])
    po: dict[str, Any] = {
        "po_id": f"PO-{number}",
        "entity": entity,
        "vendor": rng.choice(VENDORS),
        "ordered_on": max(start, received - timedelta(days=rng.randint(3, 12))),
        "amount": cents(rng, 800.0, 48000.0),
        "approved_by": rng.choice(book.approvers),
    }
    book.pos.append(po)
    book.receipts.append(
        {"po_id": po["po_id"], "entity": entity, "received_on": received, "amount": po["amount"]}
    )
    amount = po["amount"]
    invoices: list[str] = []
    entry: dict[str, Any] = {
        "kind": kind,
        "month_end": month_end,
        "entity": entity,
        "po_id": po["po_id"],
    }
    if kind == "cross_month":
        billed = month_end + timedelta(days=rng.randint(3, 9))
        if billed <= end:
            invoices.append(book.invoice(po, billed, amount, entity))
    elif kind == "partial":
        half = round(amount / 2, 2)
        invoices.append(book.invoice(po, month_end - timedelta(days=4), half, entity))
        rest = month_end + timedelta(days=8)
        if rest <= end:
            invoices.append(book.invoice(po, rest, round(amount - half, 2), entity))
        entry["accrued"] = round(amount - half, 2)
    elif kind == "over_invoiced":
        billed_amount = round(amount * (1 + rng.uniform(0.05, 0.25)), 2)
        invoices.append(book.invoice(po, month_end - timedelta(days=3), billed_amount, entity))
        entry["excess"] = round(billed_amount - amount, 2)
    else:  # wrong_entity
        other = book.entities[1] if entity == book.entities[0] else book.entities[0]
        invoices.append(book.invoice(po, month_end - timedelta(days=1), amount, other))
        entry["invoice_entity"] = other
    entry["invoices"] = invoices
    return entry


def over_invoiced(book: Book, month_ends: list[date]) -> dict[str, list[dict[str, Any]]]:
    """Per month end, the POs invoiced (to their own entity) above their receipt."""
    out: dict[str, list[dict[str, Any]]] = {}
    for me in month_ends:
        rows = []
        for r in book.receipts:
            if r["received_on"] > me:
                continue
            excess = round(billed_by(book, r, me) - r["amount"], 2)
            if excess > 0.005:
                rows.append({"po_id": r["po_id"], "entity": r["entity"], "excess": excess})
        out[me.isoformat()] = sorted(rows, key=lambda x: (x["entity"], x["po_id"]))
    return out


def _questions(month_ends: list[date]) -> tuple[ScriptedQuestion, ...]:
    over = tuple(
        ScriptedQuestion(
            f"ap.over_invoiced_{me.isoformat()}",
            "over_invoiced",
            f"Which POs had been invoiced above their receipt by {me.isoformat()}, by entity, "
            "and by how much?",
            f"""
                SELECT r.entity, r.po_id, round(sum(i.amount) - r.amount, 2) AS excess
                FROM fact_po_receipt r JOIN fact_invoice i
                  ON i.po_id = r.po_id AND i.entity = r.entity
                 AND i.received_on <= DATE '{me.isoformat()}'
                WHERE r.received_on <= DATE '{me.isoformat()}'
                GROUP BY r.entity, r.po_id, r.amount
                HAVING round(sum(i.amount) - r.amount, 2) > 0
                ORDER BY r.entity, r.po_id
                """,
        )
        for me in month_ends
    )
    wrong = ScriptedQuestion(
        "ap.wrong_entity",
        "entity_scope",
        "Which invoices were booked to another entity than their purchase order's?",
        """
            SELECT i.invoice_id, i.po_id, p.entity AS po_entity, i.entity AS invoice_entity
            FROM fact_invoice i JOIN dim_po p ON p.po_id = i.po_id
            WHERE i.entity <> p.entity
            ORDER BY i.invoice_id
            """,
    )
    return (*accrual_questions(month_ends), *over, wrong)


class AccountsPayableMonthEnd:
    name = "ap_month_end"
    title = "Accounts Payable: Every Trap at Every Month End"
    brief = (
        "The ap book of two legal entities, with a cross-month, a partial, an over-invoiced "
        "and a wrong-entity purchase order for each entity at every month end; month-end "
        "accruals and over-invoicing by entity."
    )

    def build_data(self, *, world: World, seed: int, start: date, end: date) -> ScriptedData:
        book = base_book(world, seed, start, end)
        rng = random.Random(f"{seed}\x00ap_month_end")
        month_ends = [
            me for me in month_ends_between(start, end) if me - timedelta(days=12) >= start
        ]
        number = 1000 + len(book.pos)
        traps = []
        for me in month_ends:
            for entity in book.entities:
                for kind in KINDS:
                    traps.append(
                        _plant(
                            book,
                            rng,
                            kind=kind,
                            month_end=me,
                            entity=entity,
                            number=number,
                            start=start,
                            end=end,
                        )
                    )
                    number += 1
        truth = {
            "entities": list(book.entities),
            "accruals_by_month_end": accruals(book, month_ends),
            "over_invoiced_by_month_end": over_invoiced(book, month_ends),
            "traps": traps,
        }
        return ScriptedData(tables=tables(book), questions=_questions(month_ends), truth=truth)


SCRIPTED_SCENARIOS.register(AccountsPayableMonthEnd())
