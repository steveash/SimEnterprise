"""The ``sales`` department template: archetype + ``run_sales`` playbook.

A sales org unit: account executives, sales engineering and revenue operations.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

SALES = register_kit(
    DepartmentKit(
        name="sales",
        charter="Win and grow customer revenue.",
        typical_goals=(
            "Close {target} of new bookings.",
            "Lift win rate to {target}.",
            "Shorten the sales cycle by {n} days.",
        ),
        team_shapes=(
            TeamShape(
                "Account Executives",
                "3..7",
                (
                    "negotiation",
                    "prospecting",
                ),
            ),
            TeamShape(
                "Sales Engineering",
                "1..3",
                (
                    "demos",
                    "solution_design",
                ),
            ),
            TeamShape(
                "Revenue Operations",
                "1..2",
                (
                    "crm",
                    "forecasting",
                ),
            ),
        ),
        lead_role="sales_director",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the sales plan for the quarter",
                "start",
                "SalesPlanSet",
            ),
            DocSpec(
                "deal",
                "deal_review",
                "review a deal in the pipeline",
                "random:1.0/week",
                "DealReviewed",
                by="member",
                review=True,
            ),
            DocSpec(
                "forecast",
                "forecast_call",
                "run the weekly forecast call",
                "cadence:weekly:MON",
                "ForecastCalled",
            ),
            DocSpec(
                "account",
                "account_plan",
                "update an account plan",
                "cadence:every:3w",
                "AccountPlanned",
                by="member",
            ),
            DocSpec(
                "winloss",
                "win_loss",
                "write a win/loss analysis",
                "random:0.5/week",
                "WinLossWritten",
                by="member",
            ),
            DocSpec(
                "pricing",
                "pricing_exception",
                "request a pricing exception",
                "random:0.3/week",
                "PricingExceptionRequested",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "SalesStatusReported",
            ),
        ),
        vertical="sales",
        verticals=(
            "sales",
            "b2b sales",
        ),
    )
)
