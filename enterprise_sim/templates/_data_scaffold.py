"""The ``data_scenario`` template skeleton (``plugin.py`` + test) — see :mod:`.scaffold`.

A scaffolded data scenario is a small, lint-clean causal spec: one demand
factor, one ``account`` population, a dimension and a daily fact table, a
weekly view, and two business questions — the second carries a *gap fix* so
the question loop has something to close. Kept in its own module because the
generated source is itself a template-with-braces, which reads far better
here than inline in :mod:`.scaffold`.
"""

from __future__ import annotations

_PLUGIN = '''"""__TITLE__: a structured-data scenario template (scaffolded).

A data scenario is a causal graph over categorical and numeric variables that is
sampled into partitioned parquet tables, materialized into SQL views, and asked
business questions (docs/DATA_PRODUCTS.md). This scaffold is a small, lint-clean
starting point: one demand factor, one ``account`` population, a dimension and a
daily fact table, a weekly view, and two questions — the second carries a *gap
fix* so the question loop has something to close. Grow it: add populations
(``kg_identity`` / ``kg_dimensions`` link rows to the world's KG), variables,
tables, views and questions. ``templates validate`` lints the spec and samples a
tiny copy of it end to end.
"""

from __future__ import annotations

from datetime import date

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
    Link,
    PopulationSpec,
    QuestionSpec,
    ScenarioSpec,
    TableGrain,
    VariableKind,
)

BRIEF = """__BRIEF__"""


def _spec(start: date, end: date) -> ScenarioSpec:
    factors = (
        FactorSpec(
            name="demand",
            description="Latent market demand driving daily activity.",
            base=0.0,
            trend_per_day=0.002,
            weekday_effects=(0.1, 0.1, 0.05, 0.05, 0.0, -0.5, -0.6),
            noise_sigma=0.05,
            ar_coef=0.8,
        ),
    )
    accounts = PopulationSpec(
        name="account",
        description="Customer accounts.",
        size=200,
        attributes=(
            variable(
                "segment",
                VariableKind.CATEGORICAL,
                dist=categorical({"smb": 3.0, "mid_market": 2.0, "enterprise": 1.0}),
            ),
            variable("engagement", VariableKind.NUMERIC, dist=normal(0.0, 1.0)),
        ),
        panel=(
            variable(
                "sessions",
                VariableKind.COUNT,
                eq=eqn(
                    0.8,
                    (
                        term("demand", 0.6),
                        term("engagement", 0.4),
                        term("segment", levels={"enterprise": 0.7, "mid_market": 0.3}),
                    ),
                    link=Link.EXP,
                ),
                description="Sessions today.",
            ),
        ),
    )
    tables = (
        table(
            "dim_account",
            "account",
            TableGrain.ENTITY,
            (entity_col("account_id"), attr_col("segment"), attr_col("engagement")),
        ),
        table(
            "fact_account_day",
            "account",
            TableGrain.ENTITY_DAY,
            (entity_col("account_id"), date_col(), panel_col("sessions")),
            description="Daily account activity.",
        ),
    )
    views = (
        view(
            "sessions_weekly",
            """
            SELECT date_trunc('week', date) AS week, sum(sessions) AS sessions
            FROM fact_account_day GROUP BY 1 ORDER BY 1
            """,
        ),
    )
    return ScenarioSpec(
        name="__SLUG__",
        title="__TITLE__",
        description=BRIEF,
        start=start,
        end=end,
        factors=factors,
        populations=(accounts,),
        tables=tables,
        views=views,
        question_categories=("activity", "segmentation"),
    )


def _questions(spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
    return (
        question(
            "__SLUG__.sessions_by_segment",
            "activity",
            "Which customer segment is the most active?",
            """
            SELECT d.segment, sum(f.sessions) AS sessions
            FROM fact_account_day f JOIN dim_account d ON d.account_id = f.account_id
            GROUP BY d.segment ORDER BY sessions DESC
            """,
        ),
        question(
            "__SLUG__.sessions_by_region",
            "segmentation",
            "How does activity differ by region?",
            """
            SELECT d.region, sum(f.sessions) AS sessions
            FROM fact_account_day f JOIN dim_account d ON d.account_id = f.account_id
            GROUP BY d.region ORDER BY sessions DESC
            """,
            gap_fix=gap(
                "Accounts carry no region; add one and expose it on dim_account.",
                gap_add_attr(
                    "account",
                    variable(
                        "region",
                        VariableKind.CATEGORICAL,
                        dist=categorical({"na": 3.0, "emea": 2.0, "apac": 1.5}),
                    ),
                ),
                gap_add_col("dim_account", attr_col("region")),
            ),
        ),
    )


class _Scenario:
    """The registered data scenario (the DataScenario protocol)."""

    name = "__SLUG__"
    title = "__TITLE__"
    brief = BRIEF

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        spec = _spec(start, end)
        return spec.model_copy(update={"questions": _questions(spec)})

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        return _questions(spec)


if "__SLUG__" not in DATA_SCENARIOS:
    DATA_SCENARIOS.register(_Scenario())
'''

_TEST = '''"""Tests for the __SLUG__ data scenario template (run by ``templates validate``)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from enterprise_sim.core.registry import discover_paths
from enterprise_sim.data_products.lint import require_clean
from enterprise_sim.data_products.scenarios import DATA_SCENARIOS

discover_paths([Path(__file__).resolve().parent.parent])


def test___SLUG___data_scenario_lints_clean() -> None:
    plugin = DATA_SCENARIOS.get("__SLUG__")
    spec = plugin.build_spec(start=date(2026, 1, 5), end=date(2026, 2, 1))
    require_clean(spec)
    assert spec.questions, "a data scenario must ship a question bank"
'''


def data_scenario_plugin(slug: str, title: str, description: str) -> str:
    """The scaffolded ``plugin.py`` source for a ``data_scenario`` template."""
    brief = (description or f"The {title} structured-data scenario.").replace('"""', "'''")
    return _PLUGIN.replace("__SLUG__", slug).replace("__TITLE__", title).replace("__BRIEF__", brief)


def data_scenario_test(slug: str) -> str:
    """The scaffolded ``test_<slug>.py`` source for a ``data_scenario`` template."""
    return _TEST.replace("__SLUG__", slug)


__all__ = ["data_scenario_plugin", "data_scenario_test"]
