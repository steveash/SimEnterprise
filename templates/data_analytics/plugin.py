"""The ``data_analytics`` department template: archetype + ``run_data_analytics`` playbook.

A data-analysis org unit: product analytics, business intelligence and data science.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

DATA_ANALYTICS = register_kit(
    DepartmentKit(
        name="data_analytics",
        charter="Turn the company's data into decisions.",
        typical_goals=(
            "Ship a single source of truth for {metric}.",
            "Answer {n} analysis requests per sprint.",
            "Raise dashboard adoption by {target}.",
        ),
        team_shapes=(
            TeamShape(
                "Product Analytics",
                "2..5",
                (
                    "sql",
                    "experimentation",
                    "python",
                ),
            ),
            TeamShape(
                "Business Intelligence",
                "2..4",
                (
                    "dashboards",
                    "metrics_layer",
                    "sql",
                ),
            ),
            TeamShape(
                "Data Science",
                "1..3",
                (
                    "forecasting",
                    "causal_inference",
                ),
            ),
        ),
        lead_role="analytics_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the analytics agenda for the half",
                "start",
                "AnalyticsAgendaSet",
            ),
            DocSpec(
                "request",
                "analysis_request",
                "request an analysis from the team",
                "random:1.0/week",
                "AnalysisRequested",
                by="member",
            ),
            DocSpec(
                "report",
                "analysis_report",
                "answer the analysis request with findings",
                "event:AnalysisRequested",
                "AnalysisDelivered",
                by="member",
                review=True,
            ),
            DocSpec(
                "metric",
                "metric_definition",
                "define or revise a business metric",
                "cadence:every:3w",
                "MetricDefined",
                review=True,
            ),
            DocSpec(
                "dashboards",
                "dashboard_changelog",
                "announce dashboard changes",
                "cadence:every:2w",
                "DashboardsChanged",
                by="member",
            ),
            DocSpec(
                "experiment",
                "experiment_readout",
                "read out an experiment",
                "random:0.4/week",
                "ExperimentReadOut",
                by="member",
                review=True,
            ),
            DocSpec(
                "business_review",
                "business_review",
                "review the month's KPIs",
                "cadence:every:4w",
                "BusinessReviewed",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "AnalyticsStatusReported",
            ),
        ),
        vertical="analytics",
        verticals=(
            "analytics",
            "data analytics",
            "business intelligence",
        ),
        ic_noun="Analyst",
    )
)
