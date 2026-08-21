"""Support & ticketing ops: agents, ticket flow, backlog, SLAs, and CSAT.

The company's real people (KG ``Person`` nodes) staff the support desk. Daily
ticket inflow rides demand, weekday rhythm, and release quality (a bad release
mid-window floods the queue); resolution capacity depends on agent efficiency
and yesterday's backlog, and satisfaction sags as backlog and handle times
grow. A second population of individual tickets carries severity, channel,
and product-area dimensions for per-ticket analytics.
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
    gap_add_view,
    kg_id_col,
    kg_name_col,
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
A customer-support operations scenario staffed by the company's real people.
Model daily ticket inflow (weekday rhythm, demand growth, and a mid-window bad
release that floods the queue), per-agent resolution throughput driven by
experience and backlog pressure, handle times, and CSAT that erodes as the
queue grows. A ticket-level table adds severity, channel, and product-area
dimensions with resolution and escalation outcomes. Deliver volume, backlog,
SLA, and satisfaction analytics with weekly rollups."""


def _spec(start: date, end: date) -> ScenarioSpec:
    window_days = (end - start).days + 1
    bad_release_on = start + timedelta(days=int(window_days * 0.5))

    factors = (
        FactorSpec(
            name="ticket_demand",
            description="Latent support-load pressure from the customer base.",
            base=0.0,
            trend_per_day=0.002,
            weekday_effects=(0.25, 0.2, 0.1, 0.1, 0.0, -0.8, -0.9),
            noise_sigma=0.08,
            ar_coef=0.75,
            shocks=(
                Shock(
                    on=bad_release_on,
                    magnitude=1.5,
                    decay=0.85,
                    description="A bad release floods the queue.",
                ),
            ),
        ),
        FactorSpec(
            name="release_quality",
            description="Rolling release quality; inverse driver of contact rate.",
            base=0.4,
            noise_sigma=0.1,
            ar_coef=0.85,
            shocks=(Shock(on=bad_release_on, magnitude=-1.2, decay=0.88),),
        ),
        FactorSpec(
            name="staffing_level",
            description="Relative staffed capacity (hiring ramps it slowly).",
            base=0.0,
            trend_per_day=0.0015,
            noise_sigma=0.03,
            ar_coef=0.6,
        ),
    )

    agents = PopulationSpec(
        name="agent",
        description="Support agents — the company's real people.",
        size=1,  # ignored: kg_identity sets the size
        kg_identity=KgIdentity(node_type="Person"),
        attributes=(
            variable("experience_years", VariableKind.NUMERIC, dist=uniform(0.2, 7.0)),
            variable(
                "efficiency",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (term("experience_years", 0.2, transform=Transform.SQRT),),
                    noise_sigma=0.3,
                ),
                description="Latent resolution efficiency.",
            ),
        ),
        panel=(
            variable(
                "tickets_opened",
                VariableKind.COUNT,
                eq=eqn(
                    1.9,
                    (term("ticket_demand", 0.5), term("release_quality", -0.4)),
                    link=Link.EXP,
                ),
                description="Tickets routed to this agent today.",
            ),
            variable(
                "tickets_resolved",
                VariableKind.COUNT,
                eq=eqn(
                    0.4,
                    (
                        term("tickets_opened", 0.55),
                        term("backlog", 0.12, lagged=True),
                        term("efficiency", 0.35),
                        term("staffing_level", 0.2),
                    ),
                    link=Link.SOFTPLUS,
                ),
                description="Tickets closed today (backlog pressure raises throughput).",
            ),
            variable(
                "backlog",
                VariableKind.NUMERIC,
                eq=eqn(
                    0.0,
                    (
                        term("backlog", 1.0, lagged=True),
                        term("tickets_opened", 1.0),
                        term("tickets_resolved", -1.0),
                    ),
                    clip_min=0.0,
                ),
                description="Open tickets carried by this agent at end of day.",
            ),
            variable(
                "avg_handle_minutes",
                VariableKind.NUMERIC,
                eq=eqn(
                    38.0,
                    (
                        term("efficiency", -8.0),
                        term("backlog", 0.35),
                    ),
                    noise_sigma=0.2,
                    noise_kind=NoiseKind.LOGNORMAL,
                    clip_min=4.0,
                ),
                description="Average minutes to handle a ticket today.",
            ),
            variable(
                "csat",
                VariableKind.NUMERIC,
                eq=eqn(
                    4.3,
                    (
                        term("backlog", -0.02),
                        term("avg_handle_minutes", -0.008),
                        term("release_quality", 0.25),
                    ),
                    noise_sigma=0.25,
                    clip_min=1.0,
                    clip_max=5.0,
                ),
                description="Mean satisfaction score (1-5) for today's resolutions.",
            ),
        ),
    )

    tickets = PopulationSpec(
        name="ticket",
        description="Individual tickets with resolution outcomes.",
        size=6000,
        kg_dimensions=(
            KgDimension(
                attribute="product_area",
                node_type="Project",
                description="The product area (KG project) the ticket concerns.",
            ),
        ),
        attributes=(
            variable(
                "severity",
                VariableKind.CATEGORICAL,
                dist=categorical({"low": 5.0, "medium": 3.0, "high": 1.5, "critical": 0.5}),
            ),
            variable(
                "channel",
                VariableKind.CATEGORICAL,
                dist=categorical({"email": 4.0, "chat": 3.5, "phone": 1.5, "portal": 1.0}),
            ),
            variable(
                "resolution_hours",
                VariableKind.NUMERIC,
                eq=eqn(
                    1.6,
                    (
                        term(
                            "severity",
                            levels={"low": -0.5, "medium": 0.0, "high": 0.7, "critical": 1.3},
                        ),
                        term("channel", levels={"phone": -0.3, "chat": -0.15}),
                        term("product_area", level_sigma=0.2),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.5,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Hours from open to resolution.",
            ),
            variable(
                "escalated",
                VariableKind.BINARY,
                eq=logit(
                    -2.2,
                    (
                        term(
                            "severity",
                            levels={"high": 1.2, "critical": 2.4, "medium": 0.3},
                        ),
                        term("resolution_hours", 0.25, transform=Transform.LOG1P),
                    ),
                ),
                description="Was the ticket escalated beyond L1?",
            ),
            variable(
                "reopened",
                VariableKind.BINARY,
                eq=logit(
                    -2.6,
                    (term("resolution_hours", -0.1, transform=Transform.LOG1P),),
                ),
                description="Was the ticket reopened after resolution?",
            ),
        ),
    )

    tables = (
        table(
            "dim_agent",
            "agent",
            TableGrain.ENTITY,
            (
                entity_col("agent_id"),
                kg_id_col(),
                kg_name_col("agent_name"),
                attr_col("experience_years"),
            ),
        ),
        table(
            "fact_agent_day",
            "agent",
            TableGrain.ENTITY_DAY,
            (
                entity_col("agent_id"),
                date_col(),
                panel_col("tickets_opened"),
                panel_col("tickets_resolved"),
                panel_col("backlog"),
                panel_col("avg_handle_minutes"),
                panel_col("csat"),
            ),
            description="Daily per-agent queue flow and satisfaction.",
        ),
        table(
            "fact_ticket",
            "ticket",
            TableGrain.ENTITY,
            (
                entity_col("ticket_id"),
                attr_col("product_area"),
                attr_col("severity"),
                attr_col("channel"),
                attr_col("resolution_hours"),
                attr_col("escalated"),
                attr_col("reopened"),
            ),
            description="One row per ticket with resolution outcomes.",
        ),
    )

    views = (
        view(
            "queue_daily",
            """
            SELECT date,
                   sum(tickets_opened) AS opened,
                   sum(tickets_resolved) AS resolved,
                   sum(backlog) AS backlog,
                   avg(csat) AS csat
            FROM fact_agent_day GROUP BY date ORDER BY date
            """,
            description="Company-level daily queue flow and CSAT.",
        ),
        view(
            "csat_weekly",
            """
            SELECT date_trunc('week', date) AS week, avg(csat) AS csat,
                   sum(tickets_resolved) AS resolved
            FROM fact_agent_day GROUP BY 1 ORDER BY 1
            """,
        ),
        view(
            "resolution_by_severity",
            """
            SELECT severity, count(*) AS tickets,
                   avg(resolution_hours) AS avg_hours,
                   avg(CASE WHEN escalated THEN 1.0 ELSE 0.0 END) AS escalation_rate
            FROM fact_ticket GROUP BY severity
            """,
        ),
        view(
            "area_health",
            """
            SELECT product_area, count(*) AS tickets,
                   avg(resolution_hours) AS avg_hours,
                   avg(CASE WHEN reopened THEN 1.0 ELSE 0.0 END) AS reopen_rate
            FROM fact_ticket GROUP BY product_area ORDER BY tickets DESC
            """,
        ),
    )

    return ScenarioSpec(
        name="support_ops",
        title="Support & Ticketing Operations",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(agents, tickets),
        tables=tables,
        views=views,
        question_categories=("volume", "efficiency", "quality", "staffing"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "so.volume_trend",
            "volume",
            "How is daily ticket volume trending, and did anything spike it?",
            "SELECT date, opened, resolved FROM queue_daily ORDER BY date",
        ),
        question(
            "so.backlog",
            "volume",
            "Is the backlog growing or shrinking over time?",
            "SELECT date, backlog FROM queue_daily ORDER BY date",
        ),
        question(
            "so.csat_trend",
            "quality",
            "How is CSAT trending week over week?",
            "SELECT week, csat FROM csat_weekly ORDER BY week",
        ),
        question(
            "so.severity_resolution",
            "efficiency",
            "How long do tickets take to resolve by severity?",
            "SELECT severity, avg_hours, escalation_rate FROM resolution_by_severity",
        ),
        question(
            "so.area_hotspots",
            "quality",
            "Which product areas generate the most tickets and reopens?",
            "SELECT product_area, tickets, reopen_rate FROM area_health",
        ),
        question(
            "so.agent_throughput",
            "efficiency",
            "Which agents resolve the most tickets per day?",
            """
            SELECT a.agent_name, avg(f.tickets_resolved) AS avg_resolved
            FROM fact_agent_day f JOIN dim_agent a ON a.agent_id = f.agent_id
            GROUP BY a.agent_name ORDER BY avg_resolved DESC
            """,
        ),
        question(
            "so.weekday_pattern",
            "volume",
            "What does the weekday inflow pattern look like?",
            """
            SELECT dayname(date) AS day, avg(tickets_opened) AS avg_opened
            FROM fact_agent_day GROUP BY dayname(date), isodow(date) ORDER BY isodow(date)
            """,
        ),
        question(
            "so.channel_mix",
            "volume",
            "What is the ticket mix across support channels?",
            "SELECT channel, count(*) AS tickets FROM fact_ticket GROUP BY channel",
        ),
        # -- gap questions --------------------------------------------------- #
        question(
            "so.sla_breach",
            "quality",
            "What share of tickets breach their severity SLA?",
            """
            SELECT severity, avg(CASE WHEN sla_breached THEN 1.0 ELSE 0.0 END) AS breach_rate
            FROM fact_ticket GROUP BY severity
            """,
            gap_fix=gap(
                "No SLA flag exists; derive a causal breach flag from resolution time.",
                gap_add_attr(
                    "ticket",
                    variable(
                        "sla_breached",
                        VariableKind.BINARY,
                        eq=logit(
                            -3.0,
                            (
                                term("resolution_hours", 0.9, transform=Transform.LOG1P),
                                term(
                                    "severity",
                                    levels={"critical": 1.5, "high": 0.8, "medium": 0.2},
                                ),
                            ),
                        ),
                        description="Did resolution exceed the severity's SLA?",
                    ),
                ),
                gap_add_col("fact_ticket", attr_col("sla_breached")),
            ),
        ),
        question(
            "so.frt_trend",
            "efficiency",
            "How is first-response time trending weekly?",
            "SELECT week, avg_frt_minutes FROM frt_weekly ORDER BY week",
            gap_fix=gap(
                "First-response time is not tracked; add a daily panel metric and rollup.",
                gap_add_panel(
                    "agent",
                    variable(
                        "frt_minutes",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            14.0,
                            (term("backlog", 0.5), term("efficiency", -3.0)),
                            noise_sigma=0.25,
                            noise_kind=NoiseKind.LOGNORMAL,
                            clip_min=1.0,
                        ),
                        description="Average first-response minutes today.",
                    ),
                ),
                gap_add_col("fact_agent_day", panel_col("frt_minutes")),
                gap_add_view(
                    view(
                        "frt_weekly",
                        """
                        SELECT date_trunc('week', date) AS week,
                               avg(frt_minutes) AS avg_frt_minutes
                        FROM fact_agent_day GROUP BY 1 ORDER BY 1
                        """,
                    )
                ),
            ),
        ),
        question(
            "so.cost_per_ticket",
            "staffing",
            "What does a resolved ticket cost us by agent?",
            """
            SELECT a.agent_name,
                   sum(f.avg_handle_minutes / 60.0 * a.hourly_cost * f.tickets_resolved)
                     / nullif(sum(f.tickets_resolved), 0) AS cost_per_ticket
            FROM fact_agent_day f JOIN dim_agent a ON a.agent_id = f.agent_id
            GROUP BY a.agent_name ORDER BY cost_per_ticket
            """,
            gap_fix=gap(
                "Agents carry no cost basis; add an experience-driven hourly cost.",
                gap_add_attr(
                    "agent",
                    variable(
                        "hourly_cost",
                        VariableKind.NUMERIC,
                        eq=eqn(
                            3.2,
                            (term("experience_years", 0.06),),
                            link=Link.EXP,
                            noise_sigma=0.1,
                            noise_kind=NoiseKind.LOGNORMAL,
                        ),
                        description="Fully-loaded hourly cost (USD).",
                    ),
                ),
                gap_add_col("dim_agent", attr_col("hourly_cost")),
            ),
        ),
    )


class SupportOpsScenario:
    """Registered plugin for the support operations scenario."""

    name = "support_ops"
    title = "Support & Ticketing Operations"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(SupportOpsScenario())
