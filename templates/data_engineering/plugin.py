"""The ``data_engineering`` department template: archetype + ``run_data_engineering`` playbook.

A data-engineering org unit: ingestion, warehouse modelling and pipeline reliability.
Declared as a :class:`~enterprise_sim.archetypes.kit.DepartmentKit`; importing
this module registers the archetype, the playbook and its processes.
"""

from __future__ import annotations

from enterprise_sim.archetypes._base import TeamShape
from enterprise_sim.archetypes.kit import DepartmentKit, DocSpec, register_kit

DATA_ENGINEERING = register_kit(
    DepartmentKit(
        name="data_engineering",
        charter="Move, model and keep fresh the company's data.",
        typical_goals=(
            "Land {source} in the warehouse with a freshness SLA of {target}.",
            "Cut failed pipeline runs by {target}.",
            "Migrate {table} to the new schema.",
        ),
        team_shapes=(
            TeamShape(
                "Ingestion",
                "3..6",
                (
                    "cdc",
                    "streaming",
                    "orchestration",
                ),
            ),
            TeamShape(
                "Warehouse Modeling",
                "2..5",
                (
                    "sql",
                    "dimensional_modeling",
                    "dbt",
                ),
            ),
            TeamShape(
                "Data Reliability",
                "1..3",
                (
                    "observability",
                    "data_quality",
                ),
            ),
        ),
        lead_role="de_lead",
        docs=(
            DocSpec(
                "kickoff",
                "kickoff_brief",
                "stand up the data platform roadmap",
                "start",
                "DataRoadmapKickedOff",
                milestone="data_roadmap_kicked_off",
            ),
            DocSpec(
                "pipeline_status",
                "pipeline_status",
                "report pipeline health and freshness",
                "cadence:weekly:MON",
                "PipelineStatusReported",
                by="member",
            ),
            DocSpec(
                "schema_change",
                "schema_change_rfc",
                "propose a schema change",
                "cadence:every:2w",
                "SchemaChangeProposed",
                by="member",
                review=True,
            ),
            DocSpec(
                "incident",
                "data_incident_report",
                "report a failed or late pipeline run",
                "random:0.6/week",
                "DataIncidentReported",
                by="member",
            ),
            DocSpec(
                "postmortem",
                "postmortem",
                "analyse the root cause of the data incident",
                "event:DataIncidentReported",
                "DataPostmortemWritten",
                review=True,
            ),
            DocSpec(
                "backfill",
                "backfill_plan",
                "plan a backfill of historical data",
                "random:0.3/week",
                "BackfillPlanned",
                by="member",
            ),
            DocSpec(
                "runbook",
                "runbook",
                "update the on-call runbook",
                "cadence:every:4w",
                "RunbookUpdated",
                by="member",
            ),
            DocSpec(
                "status",
                "status_report",
                "report weekly progress",
                "cadence:weekly:FRI",
                "DataStatusReported",
            ),
        ),
        vertical="data",
        verticals=(
            "data",
            "data platform",
            "data infrastructure",
        ),
        ic_noun="Data Engineer",
    )
)
