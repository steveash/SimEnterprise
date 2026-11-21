"""The ``support`` department template: archetype + ``run_support`` playbook.

A customer-support org unit: frontline, escalations and support operations.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

SUPPORT = register_kit(
    DepartmentKit(
        name="support",
        charter="Resolve customer issues fast and keep customers happy.",
        typical_goals=(
            "Hold first response under {target}.",
            "Lift CSAT to {target}.",
            "Deflect {target} of tickets with the knowledge base.",
        ),
        team_shapes=(
            TeamShape(
                "Frontline Support",
                "3..8",
                (
                    "troubleshooting",
                    "customer_service",
                ),
            ),
            TeamShape(
                "Escalations",
                "2..4",
                (
                    "debugging",
                    "incident_comms",
                ),
            ),
            TeamShape(
                "Support Operations",
                "1..2",
                (
                    "tooling",
                    "workforce_planning",
                ),
            ),
        ),
        lead_role="support_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the support goals for the quarter",
                "start",
                "SupportGoalsSet",
            ),
            DocSpec(
                "escalation",
                "escalation_note",
                "escalate a customer issue",
                "random:1.5/week",
                "IssueEscalated",
                by="member",
            ),
            DocSpec(
                "queue",
                "queue_report",
                "report the support queue",
                "cadence:weekly:MON",
                "QueueReported",
                by="member",
            ),
            DocSpec(
                "kb",
                "kb_article",
                "write a knowledge-base article",
                "random:1.0/week",
                "KbArticleWritten",
                by="member",
                review=True,
            ),
            DocSpec(
                "csat",
                "csat_review",
                "review customer satisfaction",
                "cadence:every:2w",
                "CsatReviewed",
            ),
            DocSpec(
                "comms",
                "incident_comms",
                "notify customers of an incident",
                "random:0.2/week",
                "CustomersNotified",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "SupportStatusReported",
            ),
        ),
        vertical="customer service",
        verticals=(
            "customer service",
            "support",
            "call center",
        ),
        ic_noun="Support Specialist",
    )
)
