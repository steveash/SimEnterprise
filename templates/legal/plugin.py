"""The ``legal`` department template: archetype + ``run_legal`` playbook.

A legal org unit: commercial contracts, privacy and corporate matters.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

LEGAL = register_kit(
    DepartmentKit(
        name="legal",
        charter="Protect the company and enable its deals.",
        typical_goals=(
            "Turn around contracts in {n} days.",
            "Complete the {framework} privacy program.",
            "Reduce outside counsel spend by {target}.",
        ),
        team_shapes=(
            TeamShape(
                "Commercial",
                "1..3",
                (
                    "contracts",
                    "negotiation",
                ),
            ),
            TeamShape(
                "Privacy",
                "1..2",
                (
                    "data_protection",
                    "policy",
                ),
            ),
            TeamShape(
                "Corporate",
                "1..2",
                (
                    "governance",
                    "equity",
                ),
            ),
        ),
        lead_role="legal_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "set the legal priorities for the year",
                "start",
                "LegalPrioritiesSet",
            ),
            DocSpec(
                "contract",
                "contract_review",
                "review a customer or vendor contract",
                "random:1.0/week",
                "ContractReviewed",
                by="member",
                review=True,
            ),
            DocSpec(
                "dpa",
                "dpa_review",
                "review a data processing agreement",
                "random:0.4/week",
                "DpaReviewed",
                by="member",
            ),
            DocSpec(
                "memo",
                "policy_memo",
                "write a legal policy memo",
                "cadence:every:4w",
                "PolicyMemoWritten",
            ),
            DocSpec(
                "hold",
                "litigation_hold",
                "issue a litigation hold notice",
                "random:0.1/week",
                "LitigationHoldIssued",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "LegalStatusReported",
            ),
        ),
        vertical="legal",
        verticals=(
            "legal",
            "law",
            "professional services",
        ),
        ic_noun="Counsel",
    )
)
