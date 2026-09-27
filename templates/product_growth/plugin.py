"""Product growth: users, engagement, and revenue for the simulated company.

The flagship scenario. A population of product users — each tied to a real
product line (KG ``Project``) and segmented by acquisition channel, region,
and device — plays out daily activity driven by latent engagement propensity,
market demand, release quality, and marketing pushes. Output metrics are the
classic product stack: DAU/WAU/MAU, sessions, minutes, revenue, support
contact rate, rolled up daily/weekly/monthly and sliced by every dimension.

Causal story (≈2 dozen nodes): acquisition channel and device shape a user's
engagement propensity; engagement and channel shape spend propensity; daily
activity is engagement + demand + release quality + strong habit (lagged
activity); sessions ride activity; revenue rides spend propensity gated by
activity and boosted by marketing pushes; support contacts rise with usage
and fall with release quality.
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
    factor_col,
    gap,
    gap_add_attr,
    gap_add_col,
    gap_add_panel,
    gap_add_view,
    interact,
    logit,
    panel_col,
    pred,
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
    VariableKind,
)

BRIEF = """\
A product analytics scenario for the company's flagship software products.
Model a population of end users whose daily engagement and spending emerge
from causal factors: acquisition channel quality, device mix, regional
differences, underlying market demand (trending, weekly + annual seasonality),
release quality (occasionally tanked by a bad release), and marketing pushes.
Emit the standard product metric stack — DAU/WAU/MAU, sessions, time in
product, revenue, support contact rate — sliceable by product line (real
products from the company's knowledge graph), channel, region, and device,
with daily/weekly/monthly materialized rollups."""


def _spec(start: date, end: date) -> ScenarioSpec:
    window_days = (end - start).days + 1
    bad_release_on = start + timedelta(days=int(window_days * 0.55))
    campaign_on = start + timedelta(days=int(window_days * 0.25))

    factors = (
        FactorSpec(
            name="market_demand",
            description="Latent demand for the product category.",
            base=0.0,
            trend_per_day=0.003,
            weekday_effects=(0.1, 0.12, 0.12, 0.1, 0.05, -0.35, -0.4),
            annual_amplitude=0.25,
            noise_sigma=0.08,
            ar_coef=0.85,
        ),
        FactorSpec(
            name="release_quality",
            description="Rolling quality of shipped releases; a bad release tanks it.",
            base=0.5,
            noise_sigma=0.12,
            ar_coef=0.9,
            shocks=(
                Shock(
                    on=bad_release_on,
                    magnitude=-1.4,
                    decay=0.9,
                    description="A bad release ships and is progressively hotfixed.",
                ),
            ),
        ),
        FactorSpec(
            name="marketing_push",
            description="Paid acquisition/awareness bursts.",
            base=0.0,
            noise_sigma=0.05,
            ar_coef=0.5,
            shocks=(
                Shock(
                    on=campaign_on,
                    magnitude=2.0,
                    decay=0.93,
                    description="A major brand campaign launches.",
                ),
            ),
        ),
    )

    users = PopulationSpec(
        name="user",
        description="End users of the company's products.",
        size=4000,
        kg_dimensions=(
            KgDimension(
                attribute="product_line",
                node_type="Project",
                description="The product (a real KG project) the user primarily uses.",
            ),
        ),
        attributes=(
            variable(
                "acquisition_channel",
                VariableKind.CATEGORICAL,
                dist=categorical({"organic": 4.0, "paid": 3.0, "referral": 2.0, "partner": 1.0}),
                description="How the user was acquired.",
            ),
            variable(
                "region",
                VariableKind.CATEGORICAL,
                dist=categorical({"amer": 5.0, "emea": 3.0, "apac": 2.0, "latam": 1.0}),
            ),
            variable(
                "device",
                VariableKind.CATEGORICAL,
                dist=categorical({"desktop": 5.0, "mobile": 4.0, "tablet": 1.0}),
            ),
            variable(
                "is_power_user",
                VariableKind.BINARY,
                dist=bernoulli(0.15),
                description="A heavy-usage archetype flag.",
            ),
            variable(
                "engagement_propensity",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (
                        term(
                            "acquisition_channel",
                            levels={
                                "organic": 0.35,
                                "referral": 0.5,
                                "paid": -0.15,
                                "partner": 0.0,
                            },
                        ),
                        term("device", levels={"desktop": 0.15, "mobile": 0.0, "tablet": -0.2}),
                        term("region", level_sigma=0.15),
                        term("product_line", level_sigma=0.2),
                        term("is_power_user", 1.1),
                    ),
                    noise_sigma=0.6,
                ),
                description="Latent per-user engagement propensity.",
            ),
            variable(
                "spend_propensity",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.4,
                    (
                        term("engagement_propensity", 0.55),
                        term(
                            "acquisition_channel",
                            levels={"organic": 0.1, "referral": 0.25, "paid": 0.05, "partner": 0.3},
                        ),
                    ),
                    link=Link.SOFTPLUS,
                    noise_sigma=0.5,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Latent willingness to pay (non-negative).",
            ),
        ),
        panel=(
            variable(
                "active",
                VariableKind.BINARY,
                eq=logit(
                    -1.3,
                    (
                        term("engagement_propensity", 0.9),
                        term("market_demand", 0.45),
                        term("release_quality", 0.35),
                        term("marketing_push", 0.25),
                        term("active", 1.5, lagged=True),
                    ),
                ),
                description="Did the user use the product today?",
            ),
            variable(
                "sessions",
                VariableKind.COUNT,
                eq=eqn(
                    -0.6,
                    (
                        term("active", 1.8),
                        term("engagement_propensity", 0.5),
                        term("market_demand", 0.15),
                    ),
                    link=Link.SOFTPLUS,
                ),
                description="Sessions today (0 for inactive users).",
            ),
            variable(
                "minutes",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (term("sessions", 9.0), term("engagement_propensity", 3.0)),
                    link=Link.SOFTPLUS,
                    noise_sigma=0.35,
                    noise_kind=NoiseKind.LOGNORMAL,
                    clip_max=600.0,
                ),
                description="Minutes in product today.",
            ),
            variable(
                "revenue",
                VariableKind.NUMERIC,
                eq=eqn(
                    -2.5,
                    (term("marketing_push", 0.2),),
                    interactions=(interact("spend_propensity", "active", 1.6),),
                    link=Link.SOFTPLUS,
                    noise_sigma=0.6,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Revenue attributed to the user today (USD).",
            ),
            variable(
                "support_contacts",
                VariableKind.COUNT,
                eq=eqn(
                    -3.2,
                    (term("sessions", 0.25), term("release_quality", -0.8)),
                    link=Link.EXP,
                ),
                description="Support touches today; spikes when release quality drops.",
            ),
        ),
    )

    tables = (
        table(
            "dim_user",
            "user",
            TableGrain.ENTITY,
            (
                entity_col("user_id"),
                attr_col("acquisition_channel"),
                attr_col("region"),
                attr_col("device"),
                attr_col("product_line"),
                attr_col("is_power_user"),
            ),
            description="One row per user with acquisition and segmentation dimensions.",
        ),
        table(
            "fact_user_day",
            "user",
            TableGrain.ENTITY_DAY,
            (
                entity_col("user_id"),
                date_col(),
                panel_col("active"),
                panel_col("sessions"),
                panel_col("minutes"),
                panel_col("revenue"),
                panel_col("support_contacts"),
                factor_col("release_quality"),
            ),
            description="Daily activity facts per user.",
        ),
    )

    views = (
        view(
            "metrics_daily",
            """
            SELECT date,
                   count(DISTINCT user_id) FILTER (WHERE active) AS dau,
                   sum(sessions) AS sessions,
                   sum(minutes) AS minutes,
                   sum(revenue) AS revenue,
                   sum(support_contacts) AS support_contacts
            FROM fact_user_day
            GROUP BY date
            ORDER BY date
            """,
            description="Company-level daily product metrics (DAU, sessions, revenue).",
        ),
        view(
            "metrics_weekly",
            """
            SELECT date_trunc('week', date) AS week,
                   count(DISTINCT user_id) FILTER (WHERE active) AS wau,
                   sum(revenue) AS revenue,
                   sum(sessions) AS sessions
            FROM fact_user_day
            GROUP BY 1
            ORDER BY 1
            """,
            description="Weekly active users and weekly revenue.",
        ),
        view(
            "metrics_monthly",
            """
            SELECT date_trunc('month', date) AS month,
                   count(DISTINCT user_id) FILTER (WHERE active) AS mau,
                   sum(revenue) AS revenue
            FROM fact_user_day
            GROUP BY 1
            ORDER BY 1
            """,
            description="Monthly active users and monthly revenue.",
        ),
        view(
            "engagement_by_product",
            """
            SELECT d.product_line,
                   f.date,
                   count(DISTINCT f.user_id) FILTER (WHERE f.active) AS dau,
                   sum(f.minutes) AS minutes
            FROM fact_user_day f
            JOIN dim_user d ON d.user_id = f.user_id
            GROUP BY 1, 2
            """,
            description="Daily engagement sliced by product line.",
        ),
        view(
            "revenue_by_channel_weekly",
            """
            SELECT d.acquisition_channel,
                   date_trunc('week', f.date) AS week,
                   sum(f.revenue) AS revenue,
                   count(DISTINCT f.user_id) FILTER (WHERE f.active) AS wau
            FROM fact_user_day f
            JOIN dim_user d ON d.user_id = f.user_id
            GROUP BY 1, 2
            """,
            description="Weekly revenue and WAU by acquisition channel.",
        ),
    )

    return ScenarioSpec(
        name="product_growth",
        title="Product Growth Analytics",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(users,),
        tables=tables,
        views=views,
        question_categories=(
            "growth",
            "engagement",
            "monetization",
            "retention",
            "quality",
        ),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "pg.dau_trend",
            "growth",
            "How is daily active usage trending over the period?",
            "SELECT date, dau FROM metrics_daily ORDER BY date",
        ),
        question(
            "pg.mau",
            "growth",
            "What is monthly active usage and monthly revenue?",
            "SELECT month, mau, revenue FROM metrics_monthly ORDER BY month",
        ),
        question(
            "pg.dau_by_product",
            "growth",
            "Which product lines drive daily active usage?",
            """
            SELECT product_line, avg(dau) AS avg_dau
            FROM engagement_by_product
            GROUP BY product_line
            ORDER BY avg_dau DESC
            """,
        ),
        question(
            "pg.sessions_by_device",
            "engagement",
            "How many sessions does an active user run per day, by device?",
            """
            SELECT d.device, avg(f.sessions) AS sessions_per_active_user
            FROM fact_user_day f JOIN dim_user d ON d.user_id = f.user_id
            WHERE f.active
            GROUP BY d.device
            """,
        ),
        question(
            "pg.minutes_by_region",
            "engagement",
            "Where do users spend the most time in product?",
            """
            SELECT d.region, avg(f.minutes) AS avg_minutes
            FROM fact_user_day f JOIN dim_user d ON d.user_id = f.user_id
            WHERE f.active
            GROUP BY d.region ORDER BY avg_minutes DESC
            """,
        ),
        question(
            "pg.revenue_by_channel",
            "monetization",
            "Which acquisition channels produce the most weekly revenue?",
            """
            SELECT acquisition_channel, sum(revenue) AS revenue
            FROM revenue_by_channel_weekly
            GROUP BY acquisition_channel ORDER BY revenue DESC
            """,
        ),
        question(
            "pg.arpu",
            "monetization",
            "What is average revenue per daily active user over time?",
            "SELECT date, revenue / nullif(dau, 0) AS arpu FROM metrics_daily ORDER BY date",
        ),
        question(
            "pg.retention_daily",
            "retention",
            "What share of yesterday's active users came back today?",
            """
            SELECT a.date,
                   count(*) FILTER (WHERE b.active) * 1.0 / count(*) AS next_day_retention
            FROM fact_user_day a
            JOIN fact_user_day b ON b.user_id = a.user_id AND b.date = a.date + 1
            WHERE a.active
            GROUP BY a.date ORDER BY a.date
            """,
        ),
        question(
            "pg.support_vs_quality",
            "quality",
            "Do support contacts rise when release quality drops?",
            """
            SELECT corr(support_contacts, release_quality) AS contact_quality_corr
            FROM fact_user_day
            """,
        ),
        # -- gap questions: unanswerable until the loop patches the spec ----- #
        question(
            "pg.revenue_by_plan",
            "monetization",
            "How does revenue split across subscription plan tiers?",
            """
            SELECT d.plan_tier, sum(f.revenue) AS revenue
            FROM fact_user_day f JOIN dim_user d ON d.user_id = f.user_id
            GROUP BY d.plan_tier ORDER BY revenue DESC
            """,
            gap_fix=gap(
                "Users carry no plan tier; add a causal plan_tier attribute and expose it.",
                gap_add_attr(
                    "user",
                    variable(
                        "plan_tier",
                        VariableKind.CATEGORICAL,
                        utilities={
                            "free": pred(1.2),
                            "pro": pred(0.0, (term("spend_propensity", 0.6),)),
                            "team": pred(-1.0, (term("spend_propensity", 0.9),)),
                        },
                        description="Subscription tier, driven by spend propensity.",
                    ),
                ),
                gap_add_col("dim_user", attr_col("plan_tier")),
            ),
        ),
        question(
            "pg.nps_weekly",
            "quality",
            "How does user-reported NPS trend week over week?",
            "SELECT week, avg_nps FROM nps_weekly ORDER BY week",
            gap_fix=gap(
                "No satisfaction signal is collected; add a daily NPS panel metric "
                "driven by engagement and release quality, and a weekly rollup.",
                gap_add_panel(
                    "user",
                    variable(
                        "nps",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            6.5,
                            (
                                term("engagement_propensity", 0.8),
                                term("release_quality", 1.2),
                            ),
                            noise_sigma=1.2,
                            clip_min=0.0,
                            clip_max=10.0,
                        ),
                        description="Daily sampled satisfaction score (0-10).",
                    ),
                ),
                gap_add_col("fact_user_day", panel_col("nps")),
                gap_add_view(
                    view(
                        "nps_weekly",
                        """
                        SELECT date_trunc('week', date) AS week, avg(nps) AS avg_nps
                        FROM fact_user_day WHERE active GROUP BY 1 ORDER BY 1
                        """,
                        description="Weekly average NPS among active users.",
                    )
                ),
            ),
        ),
        question(
            "pg.churn_by_cohort",
            "retention",
            "Which signup cohorts retain worst late in the period?",
            """
            SELECT d.signup_cohort,
                   count(DISTINCT f.user_id) FILTER (WHERE f.active) * 1.0
                     / count(DISTINCT f.user_id) AS active_share
            FROM fact_user_day f JOIN dim_user d ON d.user_id = f.user_id
            GROUP BY d.signup_cohort
            """,
            gap_fix=gap(
                "Users carry no signup cohort; add one correlated with channel.",
                gap_add_attr(
                    "user",
                    variable(
                        "signup_cohort",
                        VariableKind.CATEGORICAL,
                        dist=categorical({"early": 3.0, "mid": 4.0, "recent": 3.0}),
                        description="Coarse signup recency cohort.",
                    ),
                ),
                gap_add_col("dim_user", attr_col("signup_cohort")),
            ),
        ),
    )


class ProductGrowthScenario:
    """Registered plugin for the product growth scenario."""

    name = "product_growth"
    title = "Product Growth Analytics"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(ProductGrowthScenario())
