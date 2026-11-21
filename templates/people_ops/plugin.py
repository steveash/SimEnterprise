"""The ``people_ops`` department template: archetype + ``run_people_ops`` playbook.

A people org unit: recruiting, business partnering and people operations.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

PEOPLE_OPS = register_kit(
    DepartmentKit(
        name="people_ops",
        charter="Hire, grow and look after the company's people.",
        typical_goals=(
            "Fill {n} open roles this half.",
            "Lift engagement scores by {target}.",
            "Cut time to hire to {n} days.",
        ),
        team_shapes=(
            TeamShape(
                "Recruiting",
                "2..4",
                (
                    "sourcing",
                    "interviewing",
                ),
            ),
            TeamShape(
                "Business Partners",
                "1..3",
                (
                    "employee_relations",
                    "performance",
                ),
            ),
            TeamShape(
                "People Systems",
                "1..3",
                (
                    "hris",
                    "payroll",
                ),
            ),
        ),
        lead_role="hr_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the hiring and people plan",
                "start",
                "PeoplePlanSet",
            ),
            DocSpec(
                "hiring",
                "hiring_plan",
                "update the hiring plan",
                "cadence:every:4w",
                "HiringPlanUpdated",
                review=True,
            ),
            DocSpec(
                "debrief",
                "candidate_debrief",
                "debrief a candidate's interview loop",
                "random:1.2/week",
                "CandidateDebriefed",
                by="member",
                review=True,
            ),
            DocSpec(
                "offer",
                "offer_approval",
                "approve an offer",
                "random:0.5/week",
                "OfferApproved",
            ),
            DocSpec(
                "performance",
                "performance_note",
                "record a performance conversation",
                "random:0.3/week",
                "PerformanceNoted",
                by="member",
            ),
            DocSpec(
                "onboarding",
                "onboarding_checklist",
                "onboard a new hire",
                "random:0.6/week",
                "NewHireOnboarded",
                by="member",
            ),
            DocSpec(
                "policy",
                "policy_update",
                "update a people policy",
                "cadence:every:6w",
                "PeoplePolicyUpdated",
                review=True,
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "PeopleStatusReported",
            ),
        ),
        vertical="hr",
        verticals=(
            "hr",
            "human resources",
            "staffing",
        ),
        ic_noun="Partner",
    )
)
