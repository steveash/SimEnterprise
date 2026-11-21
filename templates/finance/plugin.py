"""The ``finance`` department template: archetype + ``run_finance`` playbook.

A finance org unit: planning and analysis, accounting and procurement.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

FINANCE = register_kit(
    DepartmentKit(
        name="finance",
        charter="Plan, account for and control the company's money.",
        typical_goals=(
            "Close the books in {n} days.",
            "Hold opex within {target} of plan.",
            "Cut invoice exceptions by {target}.",
        ),
        team_shapes=(
            TeamShape(
                "Planning and Analysis",
                "2..4",
                (
                    "forecasting",
                    "modeling",
                ),
            ),
            TeamShape(
                "Accounting",
                "2..5",
                (
                    "close",
                    "reconciliation",
                ),
            ),
            TeamShape(
                "Procurement",
                "1..3",
                (
                    "vendor_management",
                    "contracts",
                ),
            ),
        ),
        lead_role="finance_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the finance calendar for the year",
                "start",
                "FinanceCalendarSet",
            ),
            DocSpec(
                "variance",
                "budget_variance",
                "explain the month's budget variance",
                "cadence:every:4w",
                "VarianceExplained",
                by="member",
            ),
            DocSpec(
                "forecast",
                "forecast_update",
                "update the rolling forecast",
                "cadence:every:2w",
                "ForecastUpdated",
                by="member",
                review=True,
            ),
            DocSpec(
                "invoice",
                "invoice_exception",
                "raise an invoice exception",
                "random:1.0/week",
                "InvoiceExceptionRaised",
                by="member",
            ),
            DocSpec(
                "purchase",
                "purchase_request",
                "request a purchase approval",
                "random:0.8/week",
                "PurchaseRequested",
                by="member",
            ),
            DocSpec(
                "close",
                "close_checklist",
                "run the month-end close checklist",
                "cadence:every:4w",
                "CloseCompleted",
            ),
            DocSpec(
                "board",
                "board_pack",
                "prepare the board finance pack",
                "cadence:every:6w",
                "BoardPackPrepared",
                review=True,
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "FinanceStatusReported",
            ),
        ),
        vertical="finance",
        verticals=(
            "finance",
            "fintech",
            "banking",
            "insurance",
        ),
    )
)
