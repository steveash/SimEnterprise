"""The ``security`` department template: archetype + ``run_security`` playbook.

A security org unit: security engineering, governance and incident response.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

SECURITY = register_kit(
    DepartmentKit(
        name="security",
        charter="Keep the company's systems and data secure and compliant.",
        typical_goals=(
            "Close all critical vulnerabilities within {target}.",
            "Pass the {framework} audit.",
            "Cut phishing click rate by {target}.",
        ),
        team_shapes=(
            TeamShape(
                "Security Engineering",
                "2..4",
                (
                    "appsec",
                    "cloud_security",
                ),
            ),
            TeamShape(
                "Governance and Risk",
                "1..3",
                (
                    "compliance",
                    "risk_management",
                ),
            ),
            TeamShape(
                "Incident Response",
                "1..3",
                (
                    "forensics",
                    "detection",
                ),
            ),
        ),
        lead_role="security_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the security program for the half",
                "start",
                "SecurityProgramSet",
            ),
            DocSpec(
                "vuln",
                "vulnerability_report",
                "report a newly found vulnerability",
                "random:0.8/week",
                "VulnerabilityReported",
                by="member",
            ),
            DocSpec(
                "risk",
                "risk_assessment",
                "assess the risk of a system change",
                "cadence:every:3w",
                "RiskAssessed",
                by="member",
                review=True,
            ),
            DocSpec(
                "access",
                "access_review",
                "review who has access to sensitive systems",
                "cadence:every:4w",
                "AccessReviewed",
                by="member",
            ),
            DocSpec(
                "incident",
                "security_incident",
                "report a security incident",
                "random:0.2/week",
                "SecurityIncidentReported",
                by="member",
            ),
            DocSpec(
                "postmortem",
                "postmortem",
                "analyse the security incident",
                "event:SecurityIncidentReported",
                "SecurityPostmortemWritten",
                review=True,
            ),
            DocSpec(
                "policy",
                "policy_update",
                "update a security policy",
                "cadence:every:6w",
                "SecurityPolicyUpdated",
                review=True,
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "SecurityStatusReported",
            ),
        ),
        vertical="security",
        verticals=(
            "security",
            "cybersecurity",
        ),
    )
)
