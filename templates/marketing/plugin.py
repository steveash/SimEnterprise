"""The ``marketing`` department template: archetype + ``run_marketing`` playbook.

A marketing org unit: demand generation, content and product marketing.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

MARKETING = register_kit(
    DepartmentKit(
        name="marketing",
        charter="Build demand and tell the company's story.",
        typical_goals=(
            "Generate {n} qualified leads.",
            "Launch {product} with a {target} awareness lift.",
            "Cut cost per lead by {target}.",
        ),
        team_shapes=(
            TeamShape(
                "Demand Generation",
                "2..4",
                (
                    "paid_media",
                    "email",
                ),
            ),
            TeamShape(
                "Content",
                "2..4",
                (
                    "writing",
                    "seo",
                ),
            ),
            TeamShape(
                "Product Marketing",
                "1..3",
                (
                    "positioning",
                    "launches",
                ),
            ),
        ),
        lead_role="marketing_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the marketing plan for the quarter",
                "start",
                "MarketingPlanSet",
            ),
            DocSpec(
                "campaign",
                "campaign_brief",
                "brief a new campaign",
                "cadence:every:2w",
                "CampaignBriefed",
                by="member",
                review=True,
            ),
            DocSpec(
                "calendar",
                "content_calendar",
                "publish the content calendar",
                "cadence:every:4w",
                "ContentCalendarPublished",
                by="member",
            ),
            DocSpec(
                "launch",
                "launch_plan",
                "plan a product launch",
                "cadence:every:6w",
                "LaunchPlanned",
                review=True,
            ),
            DocSpec(
                "results",
                "campaign_results",
                "report campaign results",
                "random:0.6/week",
                "CampaignResultsReported",
                by="member",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "MarketingStatusReported",
            ),
        ),
        vertical="marketing",
        verticals=(
            "marketing",
            "media",
            "advertising",
        ),
        ic_noun="Marketer",
    )
)
