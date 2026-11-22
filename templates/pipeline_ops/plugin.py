"""Pipeline operations: job runs, failures, rows, durations and freshness.

A data-engineering telemetry scenario. A fleet of pipelines (each owned by one
of the company's real teams) runs on a schedule; daily runs, failures, rows
loaded, run duration and data freshness emerge from data volume, platform
health and each pipeline's schedule and criticality. Two incidents shape the
window: a breaking schema change a third of the way in (failures spike, rows
dip, freshness slips) and a backfill two thirds in (rows and durations spike).
A run-level table carries the failure reason of every failed run, so incident
reports and postmortems in the corpus can be checked against telemetry.
"""

from __future__ import annotations

from datetime import date, timedelta

from enterprise_sim.data_products.scenarios import DATA_SCENARIOS
from enterprise_sim.data_products.scenarios._helpers import (
    attr_col,
    categorical,
    date_col,
    entity_col,
    eqn,
    gap,
    gap_add_attr,
    gap_add_col,
    logit,
    panel_col,
    question,
    table,
    term,
    uniform,
    variable,
    view,
)
from enterprise_sim.data_products.spec import (
    FactorSpec,
    KgDimension,
    Link,
    NoiseKind,
    PopulationSpec,
    QuestionSpec,
    ScenarioSpec,
    Shock,
    TableGrain,
    Transform,
    VariableKind,
)

BRIEF = """\
A data-engineering telemetry scenario. Pipelines owned by the company's real
teams run on schedules; daily runs, failures, rows loaded, durations and data
freshness emerge from data volume, platform health, schedule and criticality.
A breaking schema change a third of the way through the window spikes
failures and freshness lag; a backfill two thirds through spikes rows and
durations. A run-level table gives every failed run its reason. Deliver
reliability, freshness, volume and cost analytics with daily and weekly
rollups."""


def _spec(start: date, end: date) -> ScenarioSpec:
    window_days = (end - start).days + 1
    schema_change_on = start + timedelta(days=int(window_days / 3))
    backfill_on = start + timedelta(days=int(window_days * 2 / 3))

    factors = (
        FactorSpec(
            name="data_volume",
            description="Upstream event volume feeding every pipeline.",
            base=0.0,
            trend_per_day=0.003,
            weekday_effects=(0.1, 0.1, 0.1, 0.1, 0.05, -0.4, -0.45),
            noise_sigma=0.05,
            ar_coef=0.7,
            shocks=(
                Shock(
                    on=backfill_on,
                    magnitude=1.6,
                    decay=0.6,
                    description="A backfill replays historical data.",
                ),
            ),
        ),
        FactorSpec(
            name="platform_health",
            description="Warehouse and orchestrator health; low means failures.",
            base=0.5,
            noise_sigma=0.08,
            ar_coef=0.8,
            shocks=(
                Shock(
                    on=schema_change_on,
                    magnitude=-1.6,
                    decay=0.75,
                    description="A breaking schema change upstream.",
                ),
            ),
        ),
    )

    pipelines = PopulationSpec(
        name="pipeline",
        description="Scheduled data pipelines, each owned by a real team.",
        size=40,
        kg_dimensions=(
            KgDimension(
                attribute="owner_team",
                node_type="Team",
                description="The KG team that owns the pipeline.",
            ),
        ),
        attributes=(
            variable(
                "schedule",
                VariableKind.CATEGORICAL,
                dist=categorical({"hourly": 3.0, "daily": 4.0, "every_15_minutes": 1.5}),
            ),
            variable(
                "criticality",
                VariableKind.CATEGORICAL,
                dist=categorical({"tier1": 1.0, "tier2": 2.0, "tier3": 3.0}),
            ),
            variable("table_width", VariableKind.NUMERIC, dist=uniform(5.0, 120.0)),
        ),
        panel=(
            variable(
                "runs",
                VariableKind.COUNT,
                eq=eqn(
                    1.0,
                    (
                        term(
                            "schedule",
                            levels={"hourly": 2.2, "every_15_minutes": 3.5, "daily": 0.0},
                        ),
                    ),
                    link=Link.EXP,
                ),
                description="Runs started today.",
            ),
            variable(
                "failures",
                VariableKind.COUNT,
                eq=eqn(
                    -1.2,
                    (
                        term("platform_health", -1.8),
                        term("runs", 0.15, transform=Transform.LOG1P),
                        term("criticality", levels={"tier1": -0.4, "tier3": 0.3}),
                    ),
                    link=Link.EXP,
                ),
                description="Runs that failed today.",
            ),
            variable(
                "rows_loaded",
                VariableKind.NUMERIC,
                eq=eqn(
                    9.0,
                    (
                        term("data_volume", 1.0),
                        term("failures", -0.15),
                        term("table_width", -0.004),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.2,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Rows written by today's successful runs.",
            ),
            variable(
                "duration_minutes",
                VariableKind.NUMERIC,
                eq=eqn(
                    2.0,
                    (
                        term("rows_loaded", 0.25, transform=Transform.LOG1P),
                        term("platform_health", -0.3),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.2,
                    noise_kind=NoiseKind.LOGNORMAL,
                    clip_min=0.5,
                ),
                description="Mean run duration today, in minutes.",
            ),
            variable(
                "freshness_hours",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.5,
                    (
                        term("freshness_hours", 0.4, lagged=True),
                        term("failures", 0.8),
                        term("schedule", levels={"daily": 6.0, "hourly": 0.5}),
                    ),
                    clip_min=0.0,
                    noise_sigma=0.3,
                ),
                description="Hours the output lags its source at end of day.",
            ),
        ),
    )

    failed_runs = PopulationSpec(
        name="failed_run",
        description="Individual failed runs with a reason.",
        size=1500,
        kg_dimensions=(
            KgDimension(
                attribute="owner_team",
                node_type="Team",
                description="The team paged for the failure.",
            ),
        ),
        attributes=(
            variable(
                "reason",
                VariableKind.CATEGORICAL,
                dist=categorical(
                    {
                        "schema_mismatch": 2.0,
                        "upstream_late": 3.0,
                        "timeout": 2.0,
                        "quota_exceeded": 1.0,
                        "bad_record": 1.5,
                    }
                ),
            ),
            variable(
                "minutes_to_recover",
                VariableKind.NUMERIC,
                eq=eqn(
                    3.5,
                    (
                        term(
                            "reason",
                            levels={"schema_mismatch": 0.8, "quota_exceeded": 0.3, "timeout": -0.3},
                        ),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.5,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Minutes from failure to the next good run.",
            ),
            variable(
                "paged",
                VariableKind.BINARY,
                eq=logit(
                    -1.0,
                    (term("minutes_to_recover", 0.4, transform=Transform.LOG1P),),
                ),
                description="Did the failure page the on-call engineer?",
            ),
        ),
    )

    tables = (
        table(
            "dim_pipeline",
            "pipeline",
            TableGrain.ENTITY,
            (
                entity_col("pipeline_id"),
                attr_col("owner_team"),
                attr_col("schedule"),
                attr_col("criticality"),
                attr_col("table_width"),
            ),
        ),
        table(
            "fact_pipeline_day",
            "pipeline",
            TableGrain.ENTITY_DAY,
            (
                entity_col("pipeline_id"),
                date_col(),
                panel_col("runs"),
                panel_col("failures"),
                panel_col("rows_loaded"),
                panel_col("duration_minutes"),
                panel_col("freshness_hours"),
            ),
            description="Daily runs, failures, volume, duration and freshness per pipeline.",
        ),
        table(
            "fact_failed_run",
            "failed_run",
            TableGrain.ENTITY,
            (
                entity_col("failed_run_id"),
                attr_col("owner_team"),
                attr_col("reason"),
                attr_col("minutes_to_recover"),
                attr_col("paged"),
            ),
            description="One row per failed run with its reason and recovery time.",
        ),
    )

    views = (
        view(
            "reliability_daily",
            """
            SELECT date, sum(runs) AS runs, sum(failures) AS failures,
                   sum(failures) * 1.0 / nullif(sum(runs), 0) AS failure_rate,
                   avg(freshness_hours) AS avg_freshness_hours
            FROM fact_pipeline_day GROUP BY date ORDER BY date
            """,
            description="Fleet-level daily reliability and freshness.",
        ),
        view(
            "volume_weekly",
            """
            SELECT date_trunc('week', date) AS week, sum(rows_loaded) AS rows_loaded,
                   avg(duration_minutes) AS avg_duration_minutes
            FROM fact_pipeline_day GROUP BY 1 ORDER BY 1
            """,
        ),
        view(
            "failure_reasons",
            """
            SELECT reason, count(*) AS failures, avg(minutes_to_recover) AS avg_recover_minutes,
                   avg(CASE WHEN paged THEN 1.0 ELSE 0.0 END) AS page_rate
            FROM fact_failed_run GROUP BY reason ORDER BY failures DESC
            """,
        ),
        view(
            "team_reliability",
            """
            SELECT p.owner_team, sum(f.failures) AS failures, sum(f.runs) AS runs,
                   avg(f.freshness_hours) AS avg_freshness_hours
            FROM fact_pipeline_day f JOIN dim_pipeline p ON p.pipeline_id = f.pipeline_id
            GROUP BY p.owner_team ORDER BY failures DESC
            """,
        ),
    )

    return ScenarioSpec(
        name="pipeline_ops",
        title="Pipeline Operations Telemetry",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(pipelines, failed_runs),
        tables=tables,
        views=views,
        question_categories=("reliability", "freshness", "volume", "cost"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "po.failure_trend",
            "reliability",
            "How is the daily failure rate trending, and when did it spike?",
            "SELECT date, failure_rate FROM reliability_daily ORDER BY date",
        ),
        question(
            "po.freshness_trend",
            "freshness",
            "How stale is the data on average each day?",
            "SELECT date, avg_freshness_hours FROM reliability_daily ORDER BY date",
        ),
        question(
            "po.volume_weekly",
            "volume",
            "How many rows did the fleet load per week?",
            "SELECT week, rows_loaded FROM volume_weekly ORDER BY week",
        ),
        question(
            "po.failure_reasons",
            "reliability",
            "What are the most common failure reasons and how long do they take to recover?",
            "SELECT reason, failures, avg_recover_minutes FROM failure_reasons",
        ),
        question(
            "po.team_reliability",
            "reliability",
            "Which teams own the least reliable pipelines?",
            "SELECT owner_team, failures, runs FROM team_reliability",
        ),
        question(
            "po.schedule_freshness",
            "freshness",
            "How does freshness differ by pipeline schedule?",
            """
            SELECT p.schedule, avg(f.freshness_hours) AS avg_freshness_hours
            FROM fact_pipeline_day f JOIN dim_pipeline p ON p.pipeline_id = f.pipeline_id
            GROUP BY p.schedule ORDER BY avg_freshness_hours
            """,
        ),
        question(
            "po.compute_cost",
            "cost",
            "What does each team spend on pipeline compute per week?",
            """
            SELECT p.owner_team, date_trunc('week', f.date) AS week,
                   sum(f.runs * f.duration_minutes / 60.0 * p.cost_per_hour) AS cost
            FROM fact_pipeline_day f JOIN dim_pipeline p ON p.pipeline_id = f.pipeline_id
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            gap_fix=gap(
                "Pipelines carry no compute price; add a width-driven hourly cost.",
                gap_add_attr(
                    "pipeline",
                    variable(
                        "cost_per_hour",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            0.5,
                            (term("table_width", 0.01),),
                            link=Link.EXP,
                            noise_sigma=0.1,
                            noise_kind=NoiseKind.LOGNORMAL,
                        ),
                        description="Compute cost per run-hour (USD).",
                    ),
                ),
                gap_add_col("dim_pipeline", attr_col("cost_per_hour")),
            ),
        ),
    )


class PipelineOpsScenario:
    """Registered plugin for the pipeline operations scenario."""

    name = "pipeline_ops"
    title = "Pipeline Operations Telemetry"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(PipelineOpsScenario())
