"""Validation suite for the full ``sell_merchandise`` retail playbook (esim-3fd52ea7).

Acceptance: the playbook lints clean (Tier 1), every declarative process passes the
built-in I1–I8 conformance suite and the playbook passes P1–P6 (Tier 2), custom
domain assertions hold, and the seeded streams are pinned by golden snapshots. The
two ``impl`` processes (``inventory_monitor``, ``purchase_order``) have no engine
runner yet, so they are covered by the static P-suite (via ``declares``), the
determinism scan, and direct unit tests of their stateful logic.

Regenerate the golden files (after an intended change) with::

    ESIM_UPDATE_GOLDEN=1 pytest tests/playbooks/test_sell_merchandise.py
"""

from __future__ import annotations

import inspect
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest
from enterprise_sim.authoring import sdk
from enterprise_sim.authoring import testkit as tk
from enterprise_sim.authoring.lint import lint_playbook, scan_impl_source
from enterprise_sim.playbooks.sell_merchandise import REORDER_POINT, SKU, sell_merchandise
from enterprise_sim.processes import inventory, purchase_order
from enterprise_sim.processes.inventory import InventoryMonitor
from enterprise_sim.processes.purchase_order import TRANSITION_EVENTS, POState, PurchaseOrder

GOLDEN_DIR = Path(__file__).parent / "golden"
START = datetime(2026, 1, 5, 9, 0)


def _process(name: str) -> sdk.Process:
    return next(a.process for a in sell_merchandise().activations if a.process.name == name)


# --------------------------------------------------------------------------- #
# Tier 1 — the playbook lints clean.
# --------------------------------------------------------------------------- #


def test_sell_merchandise_lints_clean() -> None:
    result = lint_playbook(sell_merchandise())
    assert result.ok, [str(d) for d in result.errors()]
    assert result.warnings() == ()


# --------------------------------------------------------------------------- #
# Tier 2 — per-process conformance (declarative) + playbook P-suite.
# --------------------------------------------------------------------------- #

_DECLARATIVE_PROCESSES = [
    "weekly_sales_ops_review",
    "demand_forecast",
    "supplier_negotiation",
]


@pytest.mark.parametrize("process_name", _DECLARATIVE_PROCESSES)
def test_declarative_process_conforms(process_name: str) -> None:
    tk.assert_conforms(tk.run_process(_process(process_name)))


def test_sell_merchandise_is_p_clean() -> None:
    assert tk.check_playbook(sell_merchandise()) == []


def test_sell_merchandise_playbook_run_conforms() -> None:
    tk.assert_conforms(tk.run_playbook(sell_merchandise()))


# --------------------------------------------------------------------------- #
# Custom domain assertions — not just the free suite.
# --------------------------------------------------------------------------- #


def test_weekly_review_has_spread_comment_thread() -> None:
    res = tk.run_process(_process("weekly_sales_ops_review"))
    # The headline deliverable is produced, and the spread yields a real comment thread.
    assert res.deliverable("sales_review") is not None
    assert res.events("SalesComment").count >= 2
    # Every comment threads back to an earlier event (I4 territory, asserted directly).
    by_id = {e.id: e for e in res.journal}
    for comment in res.events("SalesComment"):
        parent_id = comment.payload.get("in_reply_to") or comment.parent_event
        assert parent_id in by_id
    # The review closes with the event that feeds the forecast.
    assert res.events("SalesReviewCompiled").count == 1


def test_demand_forecast_publishes_and_sets_reorder_point() -> None:
    res = tk.run_process(_process("demand_forecast"))
    assert res.deliverable("demand_forecast") is not None
    assert res.events("ForecastPublished").count == 1
    node = res.world.get_node(SKU)
    assert node is not None
    assert node.props.get("reorder_point") == REORDER_POINT


def test_supplier_negotiation_yields_rfq_and_closes() -> None:
    res = tk.run_process(_process("supplier_negotiation"))
    assert res.deliverable("rfq") is not None
    assert res.events("NegotiationClosed").count == 1


def test_deliverable_expectations_are_all_covered() -> None:
    pb = sell_merchandise()
    declared: set[str] = set()
    for act in pb.activations:
        declared.update(act.process.declares.deliverables)
    assert set(pb.deliverable_expectations) <= declared


def test_external_supplier_role_is_marked_external() -> None:
    supplier = next(r for r in sell_merchandise().roles if r.name == "supplier")
    assert supplier.select is not None
    assert supplier.select.external is True


# --------------------------------------------------------------------------- #
# Tier 3 — structural realism metrics grade the run clean.
# --------------------------------------------------------------------------- #


def test_sell_merchandise_eval_grades_clean() -> None:
    from enterprise_sim.authoring.eval import evaluate

    report = evaluate(tk.run_playbook(sell_merchandise()).journal)
    assert report.ok, [str(m) for m in report.metrics if not m.passed]


# --------------------------------------------------------------------------- #
# Golden snapshots — seeded streams stay stable across runs.
# --------------------------------------------------------------------------- #


def test_weekly_review_golden() -> None:
    res = tk.run_process(_process("weekly_sales_ops_review"))
    tk.assert_golden(res, GOLDEN_DIR / "sell_merchandise_weekly_review.jsonl")


def test_sell_merchandise_playbook_golden() -> None:
    res = tk.run_playbook(sell_merchandise())
    tk.assert_golden(res, GOLDEN_DIR / "sell_merchandise.jsonl")


def test_sell_merchandise_run_is_deterministic() -> None:
    a = tk.run_playbook(sell_merchandise())
    b = tk.run_playbook(sell_merchandise())
    assert a.snapshot() == b.snapshot()


# --------------------------------------------------------------------------- #
# impl processes — determinism scan + stateful-logic unit tests.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", [inventory, purchase_order])
def test_impl_source_is_deterministic(module: ModuleType) -> None:
    assert scan_impl_source(inspect.getsource(module)) == []


def test_inventory_monitor_emits_low_stock_edge_triggered() -> None:
    monitor = InventoryMonitor(sku=SKU, initial_stock=60, reorder_point=REORDER_POINT, seed=1)
    events = monitor.low_stock_events(START, days=84)
    # Stock starts well above reorder and only dips below it as demand accumulates,
    # so there is at least one LowStock, and each fires at/below the reorder point.
    assert events
    assert all(r.low and r.stock_level <= REORDER_POINT for r in events)
    # Edge-triggered: never two consecutive days without a recovery between them.
    days = [r.day for r in events]
    assert days == sorted(days)
    assert len(set(days)) == len(days)


def test_inventory_monitor_is_deterministic() -> None:
    a = InventoryMonitor(seed=1).low_stock_events(START, days=84)
    b = InventoryMonitor(seed=1).low_stock_events(START, days=84)
    assert [(r.day, r.stock_level) for r in a] == [(r.day, r.stock_level) for r in b]


def test_purchase_order_lifecycle_is_ordered_and_restocks() -> None:
    transitions = list(
        PurchaseOrder(sku=SKU, supplier="supplier:acme", quantity=100, seed=1).lifecycle(START)
    )
    states = [t.state for t in transitions]
    assert states == [POState.DRAFT, POState.APPROVED, POState.SENT, POState.RECEIVED]
    # Events match the declared contract, in order.
    assert [t.event_type for t in transitions] == [TRANSITION_EVENTS[s] for s in states]
    # Timestamps are monotonic and only the terminal receipt replenishes stock.
    stamps = [t.timestamp for t in transitions]
    assert stamps == sorted(stamps)
    assert [t.stock_delta for t in transitions] == [0, 0, 0, 100]


def test_purchase_order_restocked_level_and_determinism() -> None:
    a = PurchaseOrder(seed=1).restocked_level(5, START)
    b = PurchaseOrder(seed=1).restocked_level(5, START)
    assert a == b == 105


def test_purchase_order_declared_events_match_impl() -> None:
    # The contract the engine trusts (declares.events) must match the impl's transitions.
    declared = set(_process("purchase_order").declares.events)
    assert declared == set(TRANSITION_EVENTS.values())
