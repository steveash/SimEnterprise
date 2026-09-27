"""Marketing attribution: campaigns, spend, funnel, and channel economics.

A portfolio of campaigns — each promoting a real product (KG ``Project``) —
spends daily budget into a funnel of impressions → clicks → conversions.
Channel, audience, and creative quality causally shape every stage; market
demand and CPM inflation move the whole portfolio; a viral moment mid-window
briefly makes social cheap. Output analytics are the classic acquisition
stack: spend pacing, CTR/CVR, CAC by channel, and product-level conversions.
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
    gap_add_col,
    gap_add_panel,
    gap_add_view,
    normal,
    panel_col,
    question,
    table,
    term,
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
A marketing analytics scenario over the company's real products. A portfolio
of campaigns across paid search, social, email, content, and events spends
daily budget into an impressions → clicks → conversions funnel. Creative
quality, audience fit, channel economics, market demand, CPM inflation, and a
mid-window viral moment causally drive every stage. Deliver spend pacing,
CTR/CVR, CAC-by-channel, and product-level conversion analytics with weekly
and monthly rollups."""


def _spec(start: date, end: date) -> ScenarioSpec:
    window_days = (end - start).days + 1
    viral_on = start + timedelta(days=int(window_days * 0.6))

    factors = (
        FactorSpec(
            name="market_demand",
            description="Category demand reaching the top of funnel.",
            base=0.0,
            trend_per_day=0.002,
            weekday_effects=(0.05, 0.08, 0.08, 0.05, 0.0, -0.15, -0.2),
            annual_amplitude=0.2,
            noise_sigma=0.06,
            ar_coef=0.85,
        ),
        FactorSpec(
            name="cpm_inflation",
            description="Ad-platform price inflation (suppresses impressions per dollar).",
            base=0.0,
            trend_per_day=0.0025,
            noise_sigma=0.04,
            ar_coef=0.9,
        ),
        FactorSpec(
            name="viral_moment",
            description="A social post goes viral; organic reach briefly explodes.",
            base=0.0,
            noise_sigma=0.03,
            ar_coef=0.3,
            shocks=(Shock(on=viral_on, magnitude=1.8, decay=0.7),),
        ),
    )

    campaigns = PopulationSpec(
        name="campaign",
        description="Marketing campaigns across channels.",
        size=240,
        kg_dimensions=(
            KgDimension(
                attribute="product",
                node_type="Project",
                description="The product (KG project) the campaign promotes.",
            ),
        ),
        attributes=(
            variable(
                "channel",
                VariableKind.CATEGORICAL,
                dist=categorical(
                    {"paid_search": 3.0, "social": 3.0, "email": 2.0, "content": 1.5, "events": 0.8}
                ),
            ),
            variable(
                "audience",
                VariableKind.CATEGORICAL,
                dist=categorical(
                    {
                        "developers": 3.0,
                        "smb_owners": 2.5,
                        "enterprise_buyers": 1.5,
                        "consumers": 2.0,
                    }
                ),
            ),
            variable(
                "creative_quality",
                VariableKind.NUMERIC,
                dist=normal(0.0, 1.0),
                description="Latent creative strength.",
            ),
            variable(
                "daily_budget",
                VariableKind.NUMERIC,
                eq=eqn(
                    5.6,
                    (
                        term(
                            "channel",
                            levels={
                                "paid_search": 0.5,
                                "social": 0.3,
                                "events": 0.8,
                                "email": -0.8,
                                "content": -0.4,
                            },
                        ),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.5,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Planned daily budget (USD).",
            ),
        ),
        panel=(
            variable(
                "spend",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (term("daily_budget", 1.0),),
                    noise_sigma=0.15,
                    noise_kind=NoiseKind.LOGNORMAL,
                    clip_min=0.0,
                ),
                description="Actual spend today (pacing noise around budget).",
            ),
            variable(
                "impressions",
                VariableKind.COUNT,
                eq=eqn(
                    3.2,
                    (
                        term("spend", 0.85, transform=Transform.LOG1P),
                        term("market_demand", 0.3),
                        term("cpm_inflation", -0.5),
                        term(
                            "channel",
                            levels={
                                "social": 0.5,
                                "email": 0.9,
                                "paid_search": 0.2,
                                "events": -1.2,
                            },
                        ),
                        term("viral_moment", 0.6),
                    ),
                    link=Link.EXP,
                ),
                description="Impressions served today.",
            ),
            variable(
                "clicks",
                VariableKind.COUNT,
                eq=eqn(
                    -1.8,
                    (
                        term("impressions", 0.9, transform=Transform.LOG1P),
                        term("creative_quality", 0.35),
                        term("audience", level_sigma=0.2),
                    ),
                    link=Link.EXP,
                ),
                description="Clicks today.",
            ),
            variable(
                "conversions",
                VariableKind.COUNT,
                eq=eqn(
                    -2.6,
                    (
                        term("clicks", 0.85, transform=Transform.LOG1P),
                        term(
                            "audience",
                            levels={
                                "developers": 0.3,
                                "smb_owners": 0.2,
                                "enterprise_buyers": -0.3,
                                "consumers": 0.0,
                            },
                        ),
                        term("market_demand", 0.2),
                        term("product", level_sigma=0.2),
                    ),
                    link=Link.EXP,
                ),
                description="Signups/purchases attributed today.",
            ),
        ),
    )

    tables = (
        table(
            "dim_campaign",
            "campaign",
            TableGrain.ENTITY,
            (
                entity_col("campaign_id"),
                attr_col("channel"),
                attr_col("audience"),
                attr_col("product"),
                attr_col("creative_quality"),
                attr_col("daily_budget"),
            ),
        ),
        table(
            "fact_campaign_day",
            "campaign",
            TableGrain.ENTITY_DAY,
            (
                entity_col("campaign_id"),
                date_col(),
                panel_col("spend"),
                panel_col("impressions"),
                panel_col("clicks"),
                panel_col("conversions"),
            ),
            description="Daily campaign funnel facts.",
        ),
    )

    views = (
        view(
            "funnel_daily",
            """
            SELECT date, sum(spend) AS spend, sum(impressions) AS impressions,
                   sum(clicks) AS clicks, sum(conversions) AS conversions
            FROM fact_campaign_day GROUP BY date ORDER BY date
            """,
            description="Company-level daily funnel.",
        ),
        view(
            "spend_by_channel_weekly",
            """
            SELECT date_trunc('week', f.date) AS week, d.channel,
                   sum(f.spend) AS spend, sum(f.conversions) AS conversions
            FROM fact_campaign_day f JOIN dim_campaign d ON d.campaign_id = f.campaign_id
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
        ),
        view(
            "cac_by_channel_monthly",
            """
            SELECT date_trunc('month', f.date) AS month, d.channel,
                   sum(f.spend) / nullif(sum(f.conversions), 0) AS cac
            FROM fact_campaign_day f JOIN dim_campaign d ON d.campaign_id = f.campaign_id
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            description="Blended acquisition cost by channel and month.",
        ),
        view(
            "ctr_by_audience",
            """
            SELECT d.audience,
                   sum(f.clicks) * 1.0 / nullif(sum(f.impressions), 0) AS ctr,
                   sum(f.conversions) * 1.0 / nullif(sum(f.clicks), 0) AS cvr
            FROM fact_campaign_day f JOIN dim_campaign d ON d.campaign_id = f.campaign_id
            GROUP BY d.audience
            """,
        ),
    )

    return ScenarioSpec(
        name="marketing_attribution",
        title="Marketing Attribution & Campaign Analytics",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(campaigns,),
        tables=tables,
        views=views,
        question_categories=("spend", "funnel", "efficiency", "attribution"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "ma.cac_by_channel",
            "efficiency",
            "Which channels acquire customers cheapest?",
            """
            SELECT channel, avg(cac) AS avg_cac
            FROM cac_by_channel_monthly GROUP BY channel ORDER BY avg_cac
            """,
        ),
        question(
            "ma.spend_pacing",
            "spend",
            "How is spend pacing weekly across channels?",
            "SELECT week, channel, spend FROM spend_by_channel_weekly ORDER BY week, channel",
        ),
        question(
            "ma.funnel_rates",
            "funnel",
            "What are our end-to-end funnel conversion rates over time?",
            """
            SELECT date,
                   clicks * 1.0 / nullif(impressions, 0) AS ctr,
                   conversions * 1.0 / nullif(clicks, 0) AS cvr
            FROM funnel_daily ORDER BY date
            """,
        ),
        question(
            "ma.audience_fit",
            "efficiency",
            "Which audiences click and convert best?",
            "SELECT audience, ctr, cvr FROM ctr_by_audience ORDER BY cvr DESC",
        ),
        question(
            "ma.product_conversions",
            "attribution",
            "Which products get the most attributed conversions?",
            """
            SELECT d.product, sum(f.conversions) AS conversions
            FROM fact_campaign_day f JOIN dim_campaign d ON d.campaign_id = f.campaign_id
            GROUP BY d.product ORDER BY conversions DESC
            """,
        ),
        question(
            "ma.creative_ctr",
            "efficiency",
            "Does creative quality actually move click-through?",
            """
            SELECT corr(d.creative_quality,
                        f.clicks * 1.0 / nullif(f.impressions, 0)) AS creative_ctr_corr
            FROM fact_campaign_day f JOIN dim_campaign d ON d.campaign_id = f.campaign_id
            WHERE f.impressions > 0
            """,
        ),
        question(
            "ma.viral_bump",
            "funnel",
            "Is the viral moment visible in the daily funnel?",
            "SELECT date, impressions, conversions FROM funnel_daily ORDER BY date",
        ),
        # -- gap questions --------------------------------------------------- #
        question(
            "ma.roas",
            "attribution",
            "What is return on ad spend by channel?",
            """
            SELECT d.channel,
                   sum(f.attributed_revenue) / nullif(sum(f.spend), 0) AS roas
            FROM fact_campaign_day f JOIN dim_campaign d ON d.campaign_id = f.campaign_id
            GROUP BY d.channel ORDER BY roas DESC
            """,
            gap_fix=gap(
                "Conversions carry no revenue; add causally-derived attributed revenue.",
                gap_add_panel(
                    "campaign",
                    variable(
                        "attributed_revenue",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            0.0,
                            (
                                term("conversions", 3.6),
                                term(
                                    "audience",
                                    levels={
                                        "enterprise_buyers": 2.0,
                                        "smb_owners": 0.6,
                                        "developers": 0.2,
                                    },
                                ),
                            ),
                            link=Link.SOFTPLUS,
                            noise_sigma=0.4,
                            noise_kind=NoiseKind.LOGNORMAL,
                        ),
                        description="Revenue attributed to today's conversions (USD).",
                    ),
                ),
                gap_add_col("fact_campaign_day", panel_col("attributed_revenue")),
            ),
        ),
        question(
            "ma.email_assists",
            "attribution",
            "How many conversions does email nurture assist weekly?",
            "SELECT week, assisted FROM email_assists_weekly ORDER BY week",
            gap_fix=gap(
                "Assisted conversions aren't tracked; add an assist metric and rollup.",
                gap_add_panel(
                    "campaign",
                    variable(
                        "assisted_conversions",
                        VariableKind.COUNT,
                        eq=eqn(
                            -2.0,
                            (
                                term("conversions", 0.6, transform=Transform.LOG1P),
                                term("channel", levels={"email": 1.4, "content": 0.8}),
                            ),
                            link=Link.EXP,
                        ),
                        description="Conversions this campaign assisted (multi-touch).",
                    ),
                ),
                gap_add_col("fact_campaign_day", panel_col("assisted_conversions")),
                gap_add_view(
                    view(
                        "email_assists_weekly",
                        """
                        SELECT date_trunc('week', f.date) AS week,
                               sum(f.assisted_conversions) AS assisted
                        FROM fact_campaign_day f
                        JOIN dim_campaign d ON d.campaign_id = f.campaign_id
                        WHERE d.channel IN ('email', 'content')
                        GROUP BY 1 ORDER BY 1
                        """,
                    )
                ),
            ),
        ),
    )


class MarketingAttributionScenario:
    """Registered plugin for the marketing attribution scenario."""

    name = "marketing_attribution"
    title = "Marketing Attribution & Campaign Analytics"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(MarketingAttributionScenario())
