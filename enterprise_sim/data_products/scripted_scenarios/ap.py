"""``ap``: accounts-payable rows (purchase orders, goods receipts, invoices) for two entities.

For sets2-cc's fg1-08 (P571225593, fin-accrual-cascade). The finance department buys
for two legal entities of the company (``<company> US`` and ``<company> UK``). Each
purchase order is approved by a real finance person, received on a date, and invoiced
by its vendor some days later. Month-end accruals are the receipts not yet invoiced at
the end of a month. Three cases make an accrual set work:

* **Cross-month:** received near a month end and invoiced in the next month, so they
  are accrued at that month end.
* **Partial invoice:** a PO billed in two invoices, the second after month end.
* **Entity trap:** one invoice booked to the other entity than its PO's. Matching by
  ``po_id`` alone would clear an accrual that, scoped by entity, is still open.

Tables:

* ``dim_po``: po_id, entity, vendor, ordered_on, amount, approved_by;
* ``fact_po_receipt``: po_id, entity, received_on, amount;
* ``fact_invoice``: invoice_id, entity, vendor, po_id, received_on, amount.

The gold ``truth.json`` lists the accruals open at each month end, by entity, and names
the three traps.
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
    ScriptedTable,
)

_VENDORS = (
    "Brightline Logistics",
    "Corvane Office Supply",
    "Halden Software",
    "Kestrel Facilities",
    "Marlow Consulting",
    "Northgate Telecom",
    "Ostrich Cloud",
    "Pemberton Legal",
)


def _cents(rng: random.Random, low: float, high: float) -> float:
    value = round(rng.uniform(low, high), 2)
    return value + 0.01 if round(value * 100) % 10 == 0 else value


def _month_ends(start: date, end: date) -> list[date]:
    out = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        following = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        last = following - timedelta(days=1)
        if start <= last <= end:
            out.append(last)
        cursor = following
    return out


class AccountsPayable:
    name = "ap"
    title = "Accounts Payable: POs, Receipts and Invoices"
    brief = (
        "Purchase orders for two legal entities, approved by real finance people, their "
        "goods receipts and vendor invoices; month-end accruals of received-not-invoiced "
        "POs, with a cross-month, a partial-invoice and a wrong-entity case."
    )

    def build_data(self, *, world: World, seed: int, start: date, end: date) -> ScriptedData:
        rng = random.Random(f"{seed}\x00ap")
        company = next(
            (str(n.props.get("name")) for n in world.nodes_by_type("Company")), "The Company"
        )
        entities = (f"{company} US", f"{company} UK")
        approvers = (
            sorted(
                str(n.props.get("name"))
                for n in world.nodes_by_type("Person")
                if n.props.get("department") == "finance" and n.props.get("name")
            )
            or sorted(str(n.props.get("name")) for n in world.nodes_by_type("Person"))[:3]
        )
        span = (end - start).days
        month_ends = _month_ends(start, end)
        pos: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        invoices: list[dict[str, Any]] = []
        n_invoice = 0

        def invoice(po: dict[str, Any], on: date, amount: float, entity: str) -> str:
            nonlocal n_invoice
            n_invoice += 1
            iid = f"INV-{n_invoice:05d}"
            invoices.append(
                {
                    "invoice_id": iid,
                    "entity": entity,
                    "vendor": po["vendor"],
                    "po_id": po["po_id"],
                    "received_on": on,
                    "amount": amount,
                }
            )
            return iid

        count = max(24, span // 3)
        for index in range(count):
            ordered = start + timedelta(days=rng.randint(0, max(1, span - 30)))
            po: dict[str, Any] = {
                "po_id": f"PO-{1000 + index}",
                "entity": entities[index % 2],
                "vendor": rng.choice(_VENDORS),
                "ordered_on": ordered,
                "amount": _cents(rng, 800.0, 48000.0),
                "approved_by": rng.choice(approvers),
            }
            pos.append(po)
            received = ordered + timedelta(days=rng.randint(3, 20))
            if received > end:
                continue
            receipts.append(
                {
                    "po_id": po["po_id"],
                    "entity": po["entity"],
                    "received_on": received,
                    "amount": po["amount"],
                }
            )
            billed = received + timedelta(days=rng.randint(2, 25))
            if billed <= end:
                invoice(po, billed, po["amount"], po["entity"])

        traps: dict[str, Any] = {}
        late = [me for me in month_ends if me + timedelta(days=10) <= end]
        if late:
            me = late[0]
            cross = pos[0]
            cross_receipt = next(r for r in receipts if r["po_id"] == cross["po_id"])
            cross_receipt["received_on"] = me - timedelta(days=2)
            for inv in invoices:
                if inv["po_id"] == cross["po_id"]:
                    inv["received_on"] = me + timedelta(days=6)
            traps["cross_month"] = {"po_id": cross["po_id"], "month_end": me}

            partial = pos[1]
            next(r for r in receipts if r["po_id"] == partial["po_id"])["received_on"] = (
                me - timedelta(days=8)
            )
            invoices[:] = [i for i in invoices if i["po_id"] != partial["po_id"]]
            half = round(partial["amount"] / 2, 2)
            first = invoice(partial, me - timedelta(days=4), half, partial["entity"])
            second = invoice(
                partial,
                me + timedelta(days=8),
                round(partial["amount"] - half, 2),
                partial["entity"],
            )
            traps["partial"] = {
                "po_id": partial["po_id"],
                "month_end": me,
                "invoices": [first, second],
            }

            wrong = pos[2]
            next(r for r in receipts if r["po_id"] == wrong["po_id"])["received_on"] = (
                me - timedelta(days=5)
            )
            invoices[:] = [i for i in invoices if i["po_id"] != wrong["po_id"]]
            other = entities[1] if wrong["entity"] == entities[0] else entities[0]
            wrong_invoice = invoice(wrong, me - timedelta(days=1), wrong["amount"], other)
            traps["wrong_entity"] = {
                "po_id": wrong["po_id"],
                "po_entity": wrong["entity"],
                "invoice_entity": other,
                "invoice_id": wrong_invoice,
                "month_end": me,
            }

        received_on = {r["po_id"]: r["received_on"] for r in receipts}
        for po in pos:  # a trap moved its receipt: an order always precedes its receipt
            if po["po_id"] in received_on and po["ordered_on"] > received_on[po["po_id"]]:
                po["ordered_on"] = received_on[po["po_id"]] - timedelta(days=7)

        accruals: dict[str, list[dict[str, Any]]] = {}
        for me in month_ends:
            open_rows = []
            for r in receipts:
                if r["received_on"] > me:
                    continue
                billed = sum(
                    i["amount"]
                    for i in invoices
                    if i["po_id"] == r["po_id"]
                    and i["entity"] == r["entity"]
                    and i["received_on"] <= me
                )
                remaining = round(r["amount"] - billed, 2)
                if remaining > 0.005:
                    open_rows.append(
                        {"po_id": r["po_id"], "entity": r["entity"], "accrual": remaining}
                    )
            accruals[me.isoformat()] = sorted(open_rows, key=lambda x: (x["entity"], x["po_id"]))

        truth = {"entities": list(entities), "accruals_by_month_end": accruals, "traps": traps}
        tables = (
            ScriptedTable(
                "dim_po",
                "Purchase orders: entity, vendor, date, amount, and the finance approver "
                "(a real person).",
                (
                    ("po_id", "string"),
                    ("entity", "string"),
                    ("vendor", "string"),
                    ("ordered_on", "date"),
                    ("amount", "float64"),
                    ("approved_by", "string"),
                ),
                tuple(pos),
                "po_id",
            ),
            ScriptedTable(
                "fact_po_receipt",
                "Goods receipts: one per received PO, scoped by entity.",
                (
                    ("po_id", "string"),
                    ("entity", "string"),
                    ("received_on", "date"),
                    ("amount", "float64"),
                ),
                tuple(sorted(receipts, key=lambda r: (r["received_on"], r["po_id"]))),
                "po_id",
            ),
            ScriptedTable(
                "fact_invoice",
                "Vendor invoices received, with the entity they were booked to and the PO "
                "they bill.",
                (
                    ("invoice_id", "string"),
                    ("entity", "string"),
                    ("vendor", "string"),
                    ("po_id", "string"),
                    ("received_on", "date"),
                    ("amount", "float64"),
                ),
                tuple(sorted(invoices, key=lambda i: i["invoice_id"])),
                "invoice_id",
            ),
        )
        questions = tuple(
            ScriptedQuestion(
                f"ap.accruals_{me.isoformat()}",
                "accruals",
                f"Which receipts were not yet invoiced at {me.isoformat()}, by entity, "
                "and for how much?",
                f"""
                SELECT r.entity, r.po_id, round(r.amount - coalesce(sum(i.amount), 0), 2) AS accrual
                FROM fact_po_receipt r LEFT JOIN fact_invoice i
                  ON i.po_id = r.po_id AND i.entity = r.entity
                 AND i.received_on <= DATE '{me.isoformat()}'
                WHERE r.received_on <= DATE '{me.isoformat()}'
                GROUP BY r.entity, r.po_id, r.amount
                HAVING round(r.amount - coalesce(sum(i.amount), 0), 2) > 0
                ORDER BY r.entity, r.po_id
                """,
            )
            for me in month_ends
        )
        return ScriptedData(tables=tables, questions=questions, truth=truth)


SCRIPTED_SCENARIOS.register(AccountsPayable())
