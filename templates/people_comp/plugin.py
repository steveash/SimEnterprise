"""People compensation: per-person Q4 bonuses, a retention payment, and the pools.

A people-ops and finance scenario (P571023243, for sets2-cc's fg1-09
fin-bonus-pool-private). Every row of the bonus table is one of the company's real
people, with their real department, team (the cost centre) and seniority. A Q4 bonus
follows from base salary, a seniority-driven target and a performance multiplier,
rounded to cents (always non-zero cents). One retention payment, due on the last day
of the January after that Q4, is the single adjustment line. Views give department
totals and the gross pool per cost centre (the bonuses plus the adjustment).
"""

from __future__ import annotations

from datetime import date

from enterprise_sim.data_products.scenarios import DATA_SCENARIOS
from enterprise_sim.data_products.scenarios._helpers import (
    attr_col,
    categorical,
    entity_col,
    eqn,
    gap,
    gap_add_attr,
    gap_add_col,
    kg_id_col,
    kg_name_col,
    question,
    table,
    term,
    uniform,
    variable,
    view,
)
from enterprise_sim.data_products.spec import (
    KgDimension,
    KgIdentity,
    Link,
    NoiseKind,
    PopulationSpec,
    QuestionSpec,
    ScenarioSpec,
    TableGrain,
    Transform,
    VariableKind,
)

BRIEF = """\
A people-operations compensation scenario. Every employee (the company's real
people, with their department, team as cost centre and seniority) earns a Q4
bonus from base salary, a seniority target and performance, in cents. One
retention payment, due at the end of January, is the only adjustment. Deliver
department bonus totals and gross pools by cost centre for finance."""


def _quarter(start: date) -> tuple[str, str]:
    """The bonus quarter's label and the retention payment's due date."""
    year = start.year - 1 if start.month <= 6 else start.year
    return f"{year}-Q4", f"{year + 1}-01-31"


def _spec(start: date, end: date) -> ScenarioSpec:
    quarter, due = _quarter(start)
    employees = PopulationSpec(
        name="employee",
        description="The company's real people, with their department, team and seniority.",
        size=1,  # ignored: kg_identity sets the size
        kg_identity=KgIdentity(node_type="Person", props=("department", "unit", "seniority")),
        attributes=(
            variable(
                "base_salary",
                VariableKind.NUMERIC,
                eq=eqn(
                    11.0,
                    (
                        term(
                            "seniority",
                            levels={"junior": -0.25, "mid": 0.0, "senior": 0.3, "principal": 0.55},
                        ),
                    ),
                    link=Link.EXP,
                    noise_sigma=0.08,
                    noise_kind=NoiseKind.LOGNORMAL,
                ),
                description="Annual base salary (USD).",
                decimals=2,
            ),
            variable(
                "performance",
                VariableKind.NUMERIC,
                dist=uniform(0.6, 1.4),
                description="Performance multiplier for the quarter.",
            ),
            variable(
                "q4_bonus",
                VariableKind.NUMERIC,
                # log(bonus) = log(base) + log(target / 4) + (performance - 1): a quarter of
                # an annual target of 8% (junior 6%, senior 12%, principal 16%), scaled by
                # performance (about 0.67x to 1.49x).
                eq=eqn(
                    -4.912,
                    (
                        term("base_salary", 1.0, transform=Transform.LOG1P),
                        term(
                            "seniority",
                            levels={"junior": -0.288, "senior": 0.405, "principal": 0.693},
                        ),
                        term("performance", 1.0),
                    ),
                    link=Link.EXP,
                ),
                description=f"Bonus for {quarter} (USD, two decimals).",
                decimals=2,
            ),
        ),
    )
    adjustments = PopulationSpec(
        name="adjustment",
        description="Adjustment lines to the bonus run: one retention payment.",
        size=1,
        kg_dimensions=(
            KgDimension(attribute="employee", node_type="Person", description="Who it is for."),
        ),
        attributes=(
            # constant columns: a second level at weight 0 is never drawn
            variable(
                "kind", VariableKind.CATEGORICAL, dist=categorical({"retention": 1.0, "other": 0.0})
            ),
            variable(
                "due_date", VariableKind.CATEGORICAL, dist=categorical({due: 1.0, "none": 0.0})
            ),
            variable(
                "amount",
                VariableKind.NUMERIC,
                dist=uniform(4000.0, 15000.0),
                description="Payment amount (USD, two decimals).",
                decimals=2,
            ),
        ),
    )
    tables = (
        table(
            "fact_employee_bonus",
            "employee",
            TableGrain.ENTITY,
            (
                entity_col("employee_id"),
                kg_id_col("person_id"),
                kg_name_col("name"),
                attr_col("department"),
                attr_col("cost_centre", "unit"),
                attr_col("seniority"),
                attr_col("base_salary"),
                attr_col("q4_bonus"),
            ),
            description=f"One row per person: the {quarter} bonus with department and cost centre.",
        ),
        table(
            "fact_bonus_adjustment",
            "adjustment",
            TableGrain.ENTITY,
            (
                entity_col("adjustment_id"),
                attr_col("employee"),
                attr_col("kind"),
                attr_col("amount"),
                attr_col("due_date"),
            ),
            description="Adjustment lines: one retention payment due in January.",
        ),
    )
    views = (
        view(
            "bonus_by_department",
            """
            SELECT department, count(*) AS people, round(sum(q4_bonus), 2) AS total_bonus
            FROM fact_employee_bonus GROUP BY department ORDER BY department
            """,
            description="Department totals of the bonus run.",
        ),
        view(
            "bonus_pool_by_cost_centre",
            """
            WITH bonus AS (
                SELECT cost_centre, sum(q4_bonus) AS bonus FROM fact_employee_bonus
                GROUP BY cost_centre
            ), adj AS (
                SELECT e.cost_centre, sum(a.amount) AS adjustment
                FROM fact_bonus_adjustment a JOIN fact_employee_bonus e ON e.name = a.employee
                GROUP BY e.cost_centre
            )
            SELECT b.cost_centre, round(b.bonus, 2) AS bonus,
                   round(coalesce(j.adjustment, 0), 2) AS adjustment,
                   round(b.bonus + coalesce(j.adjustment, 0), 2) AS gross_pool
            FROM bonus b LEFT JOIN adj j ON j.cost_centre = b.cost_centre
            ORDER BY b.cost_centre
            """,
            description="Gross bonus pool per cost centre: bonuses plus adjustments.",
        ),
    )
    return ScenarioSpec(
        name="people_comp",
        title="People Compensation: Q4 Bonuses",
        description=BRIEF,
        start=start,
        end=end,
        factors=(),
        populations=(employees, adjustments),
        tables=tables,
        views=views,
        question_categories=("compensation", "finance"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "pc.department_totals",
            "compensation",
            "What is the total Q4 bonus per department?",
            "SELECT department, total_bonus FROM bonus_by_department",
        ),
        question(
            "pc.cost_centre_pools",
            "finance",
            "What is the gross bonus pool per cost centre, including adjustments?",
            "SELECT cost_centre, gross_pool FROM bonus_pool_by_cost_centre",
        ),
        question(
            "pc.retention",
            "finance",
            "Which retention payment is due, to whom, for how much and when?",
            "SELECT employee, amount, due_date FROM fact_bonus_adjustment WHERE kind = 'retention'",
        ),
        question(
            "pc.top_bonus",
            "compensation",
            "Who received the largest Q4 bonus in each department?",
            """
            SELECT department, name, q4_bonus FROM (
                SELECT department, name, q4_bonus,
                       row_number() OVER (PARTITION BY department ORDER BY q4_bonus DESC) AS r
                FROM fact_employee_bonus
            ) WHERE r = 1 ORDER BY department
            """,
        ),
        question(
            "pc.tenure",
            "compensation",
            "Do longer-tenured people receive larger bonuses?",
            """
            SELECT CASE WHEN tenure_years < 2 THEN 'under 2' WHEN tenure_years < 5 THEN '2 to 5'
                        ELSE '5 and over' END AS tenure_band,
                   count(*) AS people, round(avg(q4_bonus), 2) AS avg_bonus
            FROM fact_employee_bonus GROUP BY 1 ORDER BY 1
            """,
            gap_fix=gap(
                "The bonus table carries no tenure; add years of service per person.",
                gap_add_attr(
                    "employee",
                    variable(
                        "tenure_years",
                        VariableKind.NUMERIC,
                        dist=uniform(0.3, 12.0),
                        description="Years of service.",
                        decimals=1,
                    ),
                ),
                gap_add_col("fact_employee_bonus", attr_col("tenure_years")),
            ),
        ),
    )


class PeopleCompScenario:
    """Registered plugin for the people compensation scenario."""

    name = "people_comp"
    title = "People Compensation: Q4 Bonuses"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


DATA_SCENARIOS.register(PeopleCompScenario())
