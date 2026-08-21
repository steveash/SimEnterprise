"""Subscription billing & finance: accounts, MRR, churn, and a price change.

A book of subscription accounts — each owned by a real team from the KG —
whose plan, seats, and industry set a base MRR. Daily retention is a hazard
driven by latent churn risk, macro conditions, and a permanent mid-window
price increase (a step shock), producing the classic finance stack: MRR/ARR,
churn, and plan-mix analytics with monthly rollups.
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
    gap_add_panel,
    gap_add_table,
    gap_add_view,
    interact,
    logit,
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
    TableSpec,
    Transform,
    VariableKind,
)

BRIEF = """\
A subscription-business finance scenario. Accounts (owned by real teams from
the company's knowledge graph) carry plan, seat count, and industry dimensions
that causally set base MRR; daily retention is a hazard shaped by churn risk,
plan stickiness, macro conditions, and a permanent price increase mid-window.
Deliver the finance metric stack — daily and monthly MRR, ARR, churn rate,
plan mix, seat economics — as materialized rollups."""


def _spec(start: date, end: date) -> ScenarioSpec:
    window_days = (end - start).days + 1
    price_increase_on = start + timedelta(days=int(window_days * 0.45))

    factors = (
        FactorSpec(
            name="macro_conditions",
            description="Macro budget pressure on customers (negative = tightening).",
            base=0.0,
            trend_per_day=-0.001,
            annual_amplitude=0.15,
            noise_sigma=0.05,
            ar_coef=0.9,
        ),
        FactorSpec(
            name="price_increase",
            description="A permanent list-price increase (step function).",
            base=0.0,
            shocks=(Shock(on=price_increase_on, magnitude=1.0, decay=1.0),),
        ),
    )

    accounts = PopulationSpec(
        name="account",
        description="Subscription customer accounts.",
        size=1500,
        kg_dimensions=(
            KgDimension(
                attribute="owner_team",
                node_type="Team",
                description="The real KG team that owns the account relationship.",
            ),
        ),
        attributes=(
            variable(
                "plan",
                VariableKind.CATEGORICAL,
                dist=categorical({"basic": 5.0, "pro": 3.0, "enterprise": 1.0}),
            ),
            variable(
                "industry",
                VariableKind.CATEGORICAL,
                dist=categorical(
                    {
                        "software": 3.0,
                        "retail": 2.5,
                        "finance": 2.0,
                        "healthcare": 1.5,
                        "other": 2.0,
                    }
                ),
            ),
            variable(
                "seats",
                VariableKind.COUNT,
                eq=eqn(
                    1.6,
                    (term("plan", levels={"basic": -0.6, "pro": 0.4, "enterprise": 1.8}),),
                    link=Link.EXP,
                ),
                description="Licensed seats.",
            ),
            variable(
                "base_mrr",
                VariableKind.NUMERIC,
                eq=eqn(
                    2.6,
                    (
                        term("seats", 0.75, transform=Transform.LOG1P),
                        term("plan", levels={"basic": 0.0, "pro": 0.8, "enterprise": 1.6}),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.15,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Contracted monthly recurring revenue (USD).",
            ),
            variable(
                "churn_risk",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (
                        term("plan", levels={"basic": 0.5, "pro": 0.0, "enterprise": -0.6}),
                        term("industry", level_sigma=0.2),
                        term("owner_team", level_sigma=0.15),
                    ),
                    noise_sigma=0.4,
                ),
                description="Latent propensity to churn.",
            ),
        ),
        panel=(
            variable(
                "active",
                VariableKind.BINARY,
                eq=logit(
                    2.0,
                    (
                        term("active", 4.5, lagged=True),
                        term("churn_risk", -0.6),
                        term("macro_conditions", 0.4),
                        term("price_increase", -0.35),
                    ),
                ),
                description="Is the subscription active today? (churn is near-absorbing)",
            ),
            variable(
                "mrr",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    interactions=(interact("base_mrr", "active", 1.0),),
                    clip_min=0.0,
                ),
                description="Recognized MRR today (0 once churned).",
            ),
            variable(
                "usage_intensity",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.5,
                    (
                        term("active", 0.6),
                        term("churn_risk", -0.2),
                        term("macro_conditions", 0.1),
                    ),
                    link=Link.SOFTPLUS,
                    noise_sigma=0.2,
                ),
                description="Normalized product usage (leading churn indicator).",
            ),
        ),
    )

    tables = (
        table(
            "dim_account",
            "account",
            TableGrain.ENTITY,
            (
                entity_col("account_id"),
                attr_col("plan"),
                attr_col("industry"),
                attr_col("owner_team"),
                attr_col("seats"),
                attr_col("base_mrr"),
            ),
        ),
        table(
            "fact_account_day",
            "account",
            TableGrain.ENTITY_DAY,
            (
                entity_col("account_id"),
                date_col(),
                panel_col("active"),
                panel_col("mrr"),
                panel_col("usage_intensity"),
            ),
            description="Daily subscription state and recognized MRR.",
        ),
    )

    views = (
        view(
            "mrr_daily",
            """
            SELECT date, sum(mrr) AS mrr,
                   count(*) FILTER (WHERE active) AS active_accounts
            FROM fact_account_day GROUP BY date ORDER BY date
            """,
            description="Company-level daily MRR and active account count.",
        ),
        view(
            "mrr_monthly",
            """
            SELECT date_trunc('month', date) AS month,
                   avg(daily_mrr) AS mrr,
                   max(daily_mrr) AS peak_mrr
            FROM (SELECT date, sum(mrr) AS daily_mrr FROM fact_account_day GROUP BY date)
            GROUP BY 1 ORDER BY 1
            """,
            description="Monthly MRR (average of daily recognized MRR).",
        ),
        view(
            "mrr_by_plan_monthly",
            """
            SELECT date_trunc('month', f.date) AS month, d.plan,
                   sum(f.mrr) / count(DISTINCT f.date) AS mrr
            FROM fact_account_day f JOIN dim_account d ON d.account_id = f.account_id
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
        ),
        view(
            "churn_monthly",
            """
            SELECT date_trunc('month', date) AS month,
                   1.0 - (count(*) FILTER (WHERE active) * 1.0 / count(*)) AS inactive_share
            FROM fact_account_day GROUP BY 1 ORDER BY 1
            """,
            description="Share of the book inactive, by month (churn proxy).",
        ),
    )

    return ScenarioSpec(
        name="subscription_finance",
        title="Subscription Billing & Finance",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(accounts,),
        tables=tables,
        views=views,
        question_categories=("revenue", "churn", "expansion", "billing"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "sf.mrr_trend",
            "revenue",
            "How is MRR trending daily, and is the price increase visible?",
            "SELECT date, mrr FROM mrr_daily ORDER BY date",
        ),
        question(
            "sf.mrr_monthly",
            "revenue",
            "What is monthly MRR and implied ARR?",
            "SELECT month, mrr, mrr * 12 AS arr FROM mrr_monthly ORDER BY month",
        ),
        question(
            "sf.plan_mix",
            "revenue",
            "How does MRR split across plans month over month?",
            "SELECT month, plan, mrr FROM mrr_by_plan_monthly ORDER BY month, plan",
        ),
        question(
            "sf.churn_trend",
            "churn",
            "Is churn accelerating over the window?",
            "SELECT month, inactive_share FROM churn_monthly ORDER BY month",
        ),
        question(
            "sf.churn_by_plan",
            "churn",
            "Which plans churn worst?",
            """
            SELECT d.plan,
                   1.0 - (count(*) FILTER (WHERE f.active) * 1.0 / count(*)) AS inactive_share
            FROM fact_account_day f JOIN dim_account d ON d.account_id = f.account_id
            GROUP BY d.plan ORDER BY inactive_share DESC
            """,
        ),
        question(
            "sf.seats_economics",
            "revenue",
            "How does MRR scale with seat count?",
            """
            SELECT CASE WHEN seats < 5 THEN 'xs' WHEN seats < 20 THEN 's'
                        WHEN seats < 100 THEN 'm' ELSE 'l' END AS seat_band,
                   avg(base_mrr) AS avg_mrr, count(*) AS accounts
            FROM dim_account GROUP BY 1 ORDER BY avg_mrr
            """,
        ),
        question(
            "sf.usage_churn_signal",
            "churn",
            "Does low product usage precede churn?",
            """
            SELECT corr(usage_intensity,
                        CASE WHEN active THEN 1.0 ELSE 0.0 END) AS usage_active_corr
            FROM fact_account_day
            """,
        ),
        question(
            "sf.team_book",
            "revenue",
            "Which owning teams manage the most MRR?",
            """
            SELECT d.owner_team, sum(f.mrr) AS mrr
            FROM fact_account_day f JOIN dim_account d ON d.account_id = f.account_id
            GROUP BY d.owner_team ORDER BY mrr DESC
            """,
        ),
        # -- gap questions --------------------------------------------------- #
        question(
            "sf.ltv_by_industry",
            "churn",
            "What is expected LTV by industry?",
            """
            SELECT d.industry,
                   avg(d.base_mrr * d.expected_lifetime_months) AS avg_ltv
            FROM dim_account d GROUP BY d.industry ORDER BY avg_ltv DESC
            """,
            gap_fix=gap(
                "No lifetime estimate exists; derive one causally from churn risk.",
                gap_add_attr(
                    "account",
                    variable(
                        "expected_lifetime_months",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            3.4,
                            (term("churn_risk", -0.5),),
                            link=Link.EXP,
                            noise_sigma=0.2,
                            noise_kind=NoiseKind.LOGNORMAL,
                            clip_min=1.0,
                        ),
                        description="Expected subscription lifetime in months.",
                    ),
                ),
                gap_add_col("dim_account", attr_col("expected_lifetime_months")),
            ),
        ),
        question(
            "sf.invoice_aging",
            "billing",
            "How long do customers take to pay, by plan?",
            """
            SELECT d.plan, avg(i.days_to_pay) AS avg_days_to_pay,
                   sum(i.amount) AS billed
            FROM fact_invoice i JOIN dim_account d ON d.account_id = i.account_id
            GROUP BY d.plan ORDER BY avg_days_to_pay DESC
            """,
            gap_fix=gap(
                "No invoice table exists; add one derived from account economics.",
                gap_add_attr(
                    "account",
                    variable(
                        "days_to_pay",
                        VariableKind.COUNT,
                        eq=eqn(
                            2.9,
                            (
                                term("plan", levels={"enterprise": 0.5, "pro": 0.1}),
                                term("churn_risk", 0.15),
                            ),
                            link=Link.EXP,
                        ),
                        description="Typical days from invoice to payment.",
                    ),
                ),
                gap_add_attr(
                    "account",
                    variable(
                        "invoice_amount",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            0.0,
                            (term("base_mrr", 1.0),),
                            noise_sigma=0.05,
                            noise_kind=NoiseKind.LOGNORMAL,
                        ),
                        description="Representative invoice amount (≈ one month's MRR).",
                    ),
                ),
                gap_add_table(
                    TableSpec(
                        name="fact_invoice",
                        description="One representative invoice per account.",
                        population="account",
                        grain=TableGrain.ENTITY,
                        columns=(
                            entity_col("account_id"),
                            attr_col("amount", "invoice_amount"),
                            attr_col("days_to_pay"),
                        ),
                    )
                ),
            ),
        ),
        question(
            "sf.expansion",
            "expansion",
            "How much expansion MRR are we adding weekly?",
            "SELECT week, expansion_mrr FROM expansion_weekly ORDER BY week",
            gap_fix=gap(
                "Expansion isn't modeled; add a daily expansion panel metric and rollup.",
                gap_add_panel(
                    "account",
                    variable(
                        "expansion_mrr",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            -4.0,
                            (
                                term("usage_intensity", 1.2),
                                term("active", 1.5),
                                term("macro_conditions", 0.3),
                            ),
                            link=Link.SOFTPLUS,
                            noise_sigma=0.4,
                            noise_kind=NoiseKind.LOGNORMAL,
                        ),
                        description="Incremental MRR added today from upsells.",
                    ),
                ),
                gap_add_col("fact_account_day", panel_col("expansion_mrr")),
                gap_add_view(
                    view(
                        "expansion_weekly",
                        """
                        SELECT date_trunc('week', date) AS week,
                               sum(expansion_mrr) AS expansion_mrr
                        FROM fact_account_day GROUP BY 1 ORDER BY 1
                        """,
                    )
                ),
            ),
        ),
    )


class SubscriptionFinanceScenario:
    """Registered plugin for the subscription finance scenario."""

    name = "subscription_finance"
    title = "Subscription Billing & Finance"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(SubscriptionFinanceScenario())
