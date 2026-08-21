"""Sales pipeline & CRM: reps, opportunities, win rates, and cycle times.

Two linked populations: the company's real people (KG ``Person`` nodes) acting
as sales reps with daily activity, and a pipeline of opportunities whose
size, discounting, competition, and outcomes are causally driven by segment,
source, owner, and deal economics. Quarter-end pushes (dated shocks derived
from the window) lift activity the way real sales orgs breathe.
"""

from __future__ import annotations

from datetime import date, timedelta

from enterprise_sim.data_products.scenarios import DATA_SCENARIOS
from enterprise_sim.data_products.scenarios._helpers import (
    attr_col,
    bernoulli,
    categorical,
    date_col,
    entity_col,
    eqn,
    gap,
    gap_add_attr,
    gap_add_col,
    gap_add_view,
    interact,
    kg_id_col,
    kg_name_col,
    logit,
    panel_col,
    pred,
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
    KgIdentity,
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
A sales analytics scenario over the company's own people and products. Reps
(real Person nodes from the knowledge graph) generate daily calls, meetings,
and pipeline, pushed by market demand and quarter-end pressure. Opportunities
carry segment, source, region, owner, and product dimensions; deal size,
discounting, competitive presence, and owner skill causally combine into win
probability, cycle time, and closed ACV. Deliver win-rate, pipeline, cycle,
and leaderboard analytics with weekly rollups."""


def _quarter_end_shocks(start: date, end: date) -> tuple[Shock, ...]:
    """A pressure spike at each calendar-quarter end inside the window."""
    shocks: list[Shock] = []
    year, month = start.year, start.month
    while date(year, month, 1) <= end:
        if month in (3, 6, 9, 12):
            next_month = date(year + (month == 12), month % 12 + 1, 1)
            quarter_end = next_month - timedelta(days=1)
            if start <= quarter_end <= end:
                shocks.append(Shock(on=quarter_end - timedelta(days=6), magnitude=1.2, decay=0.75))
        year, month = year + (month == 12), month % 12 + 1
    return tuple(shocks)


def _spec(start: date, end: date) -> ScenarioSpec:
    factors = (
        FactorSpec(
            name="demand",
            description="Buyer-side demand for the product category.",
            base=0.0,
            trend_per_day=0.002,
            weekday_effects=(0.15, 0.2, 0.2, 0.15, 0.0, -0.9, -0.9),
            annual_amplitude=0.2,
            noise_sigma=0.07,
            ar_coef=0.8,
        ),
        FactorSpec(
            name="quarter_end_push",
            description="Sales-org pressure spiking before each quarter close.",
            base=0.0,
            noise_sigma=0.04,
            ar_coef=0.4,
            shocks=_quarter_end_shocks(start, end),
        ),
    )

    reps = PopulationSpec(
        name="rep",
        description="Sales reps — the company's real people.",
        size=1,  # ignored: kg_identity sets the size
        kg_identity=KgIdentity(node_type="Person"),
        attributes=(
            variable("tenure_years", VariableKind.NUMERIC, dist=uniform(0.3, 9.0)),
            variable(
                "skill",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0, (term("tenure_years", 0.15, transform=Transform.SQRT),), noise_sigma=0.4
                ),
                description="Latent selling skill, growing with tenure.",
            ),
        ),
        panel=(
            variable(
                "calls",
                VariableKind.COUNT,
                eq=eqn(
                    2.2,
                    (
                        term("demand", 0.2),
                        term("quarter_end_push", 0.35),
                        term("skill", 0.1),
                    ),
                    link=Link.EXP,
                ),
                description="Outbound calls made today.",
            ),
            variable(
                "meetings",
                VariableKind.COUNT,
                eq=eqn(
                    -1.1,
                    (term("calls", 0.12), term("skill", 0.25), term("demand", 0.15)),
                    link=Link.EXP,
                ),
            ),
            variable(
                "pipeline_added",
                VariableKind.NUMERIC,
                eq=eqn(
                    6.0,
                    (
                        term("meetings", 0.35),
                        term("skill", 0.3),
                        term("quarter_end_push", 0.25),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.5,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="New pipeline value sourced today (USD).",
            ),
        ),
    )

    opportunities = PopulationSpec(
        name="opportunity",
        description="Sales opportunities worked during the window.",
        size=2500,
        kg_dimensions=(
            KgDimension(
                attribute="product",
                node_type="Project",
                description="The product (KG project) being sold.",
            ),
            KgDimension(
                attribute="owner",
                node_type="Person",
                description="The owning rep (KG person).",
            ),
        ),
        attributes=(
            variable(
                "segment",
                VariableKind.CATEGORICAL,
                dist=categorical({"smb": 5.0, "mid_market": 3.0, "enterprise": 2.0}),
            ),
            variable(
                "source",
                VariableKind.CATEGORICAL,
                dist=categorical({"inbound": 4.0, "outbound": 3.0, "partner": 2.0, "event": 1.0}),
            ),
            variable(
                "region",
                VariableKind.CATEGORICAL,
                dist=categorical({"amer": 5.0, "emea": 3.0, "apac": 2.0}),
            ),
            variable("competitor_present", VariableKind.BINARY, dist=bernoulli(0.35)),
            variable(
                "deal_size",
                VariableKind.NUMERIC,
                eq=eqn(
                    8.6,
                    (
                        term(
                            "segment",
                            levels={"smb": -1.1, "mid_market": 0.0, "enterprise": 1.4},
                        ),
                        term("source", levels={"partner": 0.3, "event": 0.15}),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.5,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Initial opportunity value (USD).",
            ),
            variable(
                "discount",
                VariableKind.NUMERIC,
                eq=eqn(
                    -1.7,
                    (
                        term("segment", levels={"enterprise": 0.8, "mid_market": 0.3}),
                        term("competitor_present", 0.7),
                    ),
                    link=Link.LOGISTIC,
                    noise_sigma=0.05,
                    clip_min=0.0,
                    clip_max=0.6,
                ),
                description="Negotiated discount fraction.",
            ),
            variable(
                "won",
                VariableKind.BINARY,
                eq=logit(
                    -0.2,
                    (
                        term("owner", level_sigma=0.3),
                        term("segment", levels={"smb": 0.25, "enterprise": -0.35}),
                        term("source", levels={"inbound": 0.3, "partner": 0.2, "outbound": -0.1}),
                        term("discount", 1.3),
                        term("competitor_present", -0.8),
                        term("deal_size", -0.08, transform=Transform.LOG1P),
                    ),
                ),
                description="Did the deal close won?",
            ),
            variable(
                "cycle_days",
                VariableKind.COUNT,
                eq=eqn(
                    2.6,
                    (
                        term("deal_size", 0.13, transform=Transform.LOG1P),
                        term("segment", levels={"enterprise": 0.35, "smb": -0.2}),
                        term("won", -0.15),
                    ),
                    link=Link.EXP,
                ),
                description="Days from creation to close.",
            ),
            variable(
                "net_price",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (term("deal_size", 1.0),),
                    interactions=(interact("deal_size", "discount", -1.0),),
                    clip_min=0.0,
                ),
                description="Deal size after discount.",
            ),
            variable(
                "acv",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    interactions=(interact("net_price", "won", 1.0),),
                    clip_min=0.0,
                ),
                description="Closed annual contract value (0 when lost).",
            ),
        ),
    )

    tables = (
        table(
            "dim_rep",
            "rep",
            TableGrain.ENTITY,
            (
                entity_col("rep_id"),
                kg_id_col(),
                kg_name_col("rep_name"),
                attr_col("tenure_years"),
            ),
            description="One row per rep, tied to a KG person.",
        ),
        table(
            "fact_rep_day",
            "rep",
            TableGrain.ENTITY_DAY,
            (
                entity_col("rep_id"),
                date_col(),
                panel_col("calls"),
                panel_col("meetings"),
                panel_col("pipeline_added"),
            ),
            description="Daily rep activity.",
        ),
        table(
            "fact_opportunity",
            "opportunity",
            TableGrain.ENTITY,
            (
                entity_col("opportunity_id"),
                attr_col("product"),
                attr_col("owner"),
                attr_col("segment"),
                attr_col("source"),
                attr_col("region"),
                attr_col("competitor_present"),
                attr_col("deal_size"),
                attr_col("discount"),
                attr_col("won"),
                attr_col("cycle_days"),
                attr_col("acv"),
            ),
            description="One row per opportunity with outcome economics.",
        ),
    )

    views = (
        view(
            "win_rate_by_segment",
            """
            SELECT segment,
                   count(*) AS opportunities,
                   avg(CASE WHEN won THEN 1.0 ELSE 0.0 END) AS win_rate,
                   sum(acv) AS acv
            FROM fact_opportunity GROUP BY segment
            """,
            description="Win rate and closed ACV by customer segment.",
        ),
        view(
            "pipeline_weekly",
            """
            SELECT date_trunc('week', date) AS week,
                   sum(pipeline_added) AS pipeline_added,
                   sum(calls) AS calls,
                   sum(meetings) AS meetings
            FROM fact_rep_day GROUP BY 1 ORDER BY 1
            """,
            description="Weekly sales activity and sourced pipeline.",
        ),
        view(
            "acv_by_product",
            """
            SELECT product, sum(acv) AS acv,
                   avg(CASE WHEN won THEN 1.0 ELSE 0.0 END) AS win_rate
            FROM fact_opportunity GROUP BY product ORDER BY acv DESC
            """,
            description="Closed ACV and win rate by product line.",
        ),
        view(
            "leaderboard",
            """
            SELECT owner, sum(acv) AS acv,
                   count(*) FILTER (WHERE won) AS deals_won
            FROM fact_opportunity GROUP BY owner ORDER BY acv DESC
            """,
            description="Rep leaderboard by closed ACV.",
        ),
    )

    return ScenarioSpec(
        name="sales_pipeline",
        title="Sales Pipeline & CRM Analytics",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(reps, opportunities),
        tables=tables,
        views=views,
        question_categories=("pipeline", "conversion", "economics", "productivity"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "sp.win_rate_segment",
            "conversion",
            "How do win rates differ across customer segments?",
            "SELECT segment, win_rate, opportunities FROM win_rate_by_segment"
            " ORDER BY win_rate DESC",
        ),
        question(
            "sp.acv_product",
            "economics",
            "Which products close the most ACV?",
            "SELECT product, acv, win_rate FROM acv_by_product",
        ),
        question(
            "sp.cycle_by_source",
            "conversion",
            "Which lead sources close fastest?",
            """
            SELECT source, avg(cycle_days) AS avg_cycle_days
            FROM fact_opportunity WHERE won GROUP BY source ORDER BY avg_cycle_days
            """,
        ),
        question(
            "sp.pipeline_trend",
            "pipeline",
            "Is sourced pipeline growing week over week, and does it spike at quarter end?",
            "SELECT week, pipeline_added FROM pipeline_weekly ORDER BY week",
        ),
        question(
            "sp.competitor_impact",
            "conversion",
            "How much does a competitor in the deal hurt win rate?",
            """
            SELECT competitor_present,
                   avg(CASE WHEN won THEN 1.0 ELSE 0.0 END) AS win_rate
            FROM fact_opportunity GROUP BY competitor_present
            """,
        ),
        question(
            "sp.leaderboard",
            "productivity",
            "Who are the top reps by closed ACV?",
            "SELECT owner, acv, deals_won FROM leaderboard LIMIT 10",
        ),
        question(
            "sp.discount_effect",
            "economics",
            "Do larger discounts actually win more deals?",
            """
            SELECT round(discount * 10) / 10 AS discount_band,
                   avg(CASE WHEN won THEN 1.0 ELSE 0.0 END) AS win_rate,
                   count(*) AS n
            FROM fact_opportunity GROUP BY 1 ORDER BY 1
            """,
        ),
        question(
            "sp.activity_per_rep",
            "productivity",
            "What does daily activity look like per rep?",
            """
            SELECT r.rep_name, avg(f.calls) AS avg_calls, avg(f.meetings) AS avg_meetings
            FROM fact_rep_day f JOIN dim_rep r ON r.rep_id = f.rep_id
            GROUP BY r.rep_name ORDER BY avg_calls DESC
            """,
        ),
        # -- gap questions --------------------------------------------------- #
        question(
            "sp.quota_attainment",
            "productivity",
            "How is each rep tracking against quota?",
            """
            SELECT r.rep_name, sum(o.acv) / any_value(r.quota) AS attainment
            FROM fact_opportunity o JOIN dim_rep r ON r.rep_name = o.owner
            GROUP BY r.rep_name ORDER BY attainment DESC
            """,
            gap_fix=gap(
                "Reps carry no quota; add a tenure-driven quota attribute.",
                gap_add_attr(
                    "rep",
                    variable(
                        "quota",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            11.6,
                            (term("tenure_years", 0.06),),
                            link=Link.EXP,
                            noise_sigma=0.2,
                            noise_kind=NoiseKind.LOGNORMAL,
                        ),
                        description="Annual quota (USD).",
                    ),
                ),
                gap_add_col("dim_rep", attr_col("quota")),
            ),
        ),
        question(
            "sp.loss_reasons",
            "conversion",
            "Why are we losing deals?",
            """
            SELECT loss_reason, count(*) AS lost
            FROM fact_opportunity WHERE NOT won
            GROUP BY loss_reason ORDER BY lost DESC
            """,
            gap_fix=gap(
                "Lost deals carry no reason; add a causal loss_reason attribute.",
                gap_add_attr(
                    "opportunity",
                    variable(
                        "loss_reason",
                        VariableKind.CATEGORICAL,
                        utilities={
                            "price": pred(0.3, (term("discount", -1.5),)),
                            "competitor": pred(-0.4, (term("competitor_present", 2.0),)),
                            "timing": pred(0.0),
                            "missing_features": pred(-0.2),
                        },
                        description="Primary loss driver (meaningful for lost deals).",
                    ),
                ),
                gap_add_col("fact_opportunity", attr_col("loss_reason")),
            ),
        ),
        question(
            "sp.funnel",
            "pipeline",
            "Where does the funnel leak — how many deals reach each stage?",
            "SELECT stage_reached, deals FROM funnel_stages ORDER BY deals DESC",
            gap_fix=gap(
                "No stage tracking exists; add a furthest-stage attribute and a funnel view.",
                gap_add_attr(
                    "opportunity",
                    variable(
                        "stage_reached",
                        VariableKind.CATEGORICAL,
                        utilities={
                            "qualified": pred(0.6, (term("won", -1.0),)),
                            "demo": pred(0.3),
                            "proposal": pred(0.1, (term("won", 0.8),)),
                            "negotiation": pred(-0.3, (term("won", 2.0),)),
                        },
                        description="Furthest pipeline stage the deal reached.",
                    ),
                ),
                gap_add_col("fact_opportunity", attr_col("stage_reached")),
                gap_add_view(
                    view(
                        "funnel_stages",
                        "SELECT stage_reached, count(*) AS deals FROM fact_opportunity GROUP BY 1",
                        description="Deal counts by furthest stage reached.",
                    )
                ),
            ),
        ),
    )


class SalesPipelineScenario:
    """Registered plugin for the sales pipeline scenario."""

    name = "sales_pipeline"
    title = "Sales Pipeline & CRM Analytics"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(SalesPipelineScenario())
