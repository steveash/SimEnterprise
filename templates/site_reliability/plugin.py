"""The ``site_reliability`` department template: archetype + ``run_site_reliability`` playbook.

A site-reliability org unit: on-call operations, release engineering and
observability. Its documents are the ones an ops team keeps: incident reports,
postmortems, on-call handoffs, change requests, runbooks and SLO reviews.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

SRE = register_kit(
    DepartmentKit(
        name="site_reliability",
        charter="Keep the company's services available, fast and safe to change.",
        typical_goals=(
            "Hold every tier-one service to its {target} availability objective.",
            "Cut mean time to restore below {target}.",
            "Retire every manual step of the release process.",
        ),
        team_shapes=(
            TeamShape(
                "On-Call Operations",
                "3..5",
                (
                    "incident_response",
                    "on_call",
                ),
            ),
            TeamShape(
                "Release Engineering",
                "2..4",
                (
                    "deploys",
                    "change_management",
                ),
            ),
            TeamShape(
                "Observability",
                "2..3",
                (
                    "monitoring",
                    "alerting",
                ),
            ),
        ),
        lead_role="site_reliability_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the reliability program for the half",
                "start",
                "ReliabilityProgramSet",
            ),
            DocSpec(
                "incident",
                "incident_report",
                "report a production incident and its timeline",
                "random:1/week",
                "IncidentReported",
                by="member",
            ),
            DocSpec(
                "postmortem",
                "postmortem",
                "write the postmortem of an incident, with its action items",
                "random:0.5/week",
                "PostmortemWritten",
                by="member",
                review=True,
            ),
            DocSpec(
                "handoff",
                "oncall_handoff",
                "hand the pager to the next on-call shift",
                "cadence:weekly:MON",
                "OnCallHandedOff",
                by="member",
            ),
            DocSpec(
                "change",
                "change_request",
                "request a production change inside a deploy window",
                "random:1/week",
                "ChangeRequested",
                by="member",
                review=True,
            ),
            DocSpec(
                "runbook",
                "runbook",
                "update a service runbook",
                "cadence:every:4w",
                "RunbookUpdated",
                by="member",
            ),
            DocSpec(
                "slo",
                "slo_review",
                "review service level objectives and error budgets",
                "cadence:every:2w",
                "SloReviewed",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly reliability status",
                "cadence:weekly:FRI",
                "ReliabilityStatusReported",
            ),
        ),
        vertical="site reliability",
        verticals=(
            "site reliability",
            "sre",
            "infrastructure operations",
        ),
        ic_noun="Site Reliability Engineer",
    )
)
