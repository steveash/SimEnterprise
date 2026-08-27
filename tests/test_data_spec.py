"""Spec model + lint tests for the data-products subsystem (docs/DATA_PRODUCTS.md).

Covers the graph-level checks Pydantic cannot see: reference resolution, cycle
detection, kind/link agreement, table/view integrity, the volume guardrail,
and the additive delta machinery the question loop relies on.
"""

from __future__ import annotations

from datetime import date

import pytest
from enterprise_sim.data_products.lint import (
    LintIssue,
    SpecLintError,
    attribute_order,
    lint_spec,
    panel_order,
    projected_rows,
    require_clean,
)
from enterprise_sim.data_products.scenarios._helpers import (
    attr_col,
    categorical,
    date_col,
    entity_col,
    eqn,
    gap_add_col,
    gap_add_view,
    logit,
    normal,
    panel_col,
    table,
    term,
    variable,
    view,
)
from enterprise_sim.data_products.spec import (
    Link,
    PopulationSpec,
    ScenarioSpec,
    SpecDelta,
    TableGrain,
    Variable,
    VariableKind,
    apply_deltas,
)

START = date(2026, 1, 5)
END = date(2026, 1, 30)


def tiny_spec(
    *,
    attributes: tuple[Variable, ...] | None = None,
    panel: tuple[Variable, ...] | None = None,
) -> ScenarioSpec:
    """A minimal valid scenario, overridable per test."""
    attrs = (
        attributes
        if attributes is not None
        else (
            variable("segment", VariableKind.CATEGORICAL, dist=categorical({"a": 1.0, "b": 1.0})),
            variable(
                "propensity",
                VariableKind.NUMERIC,
                eq=eqn(0.0, (term("segment", levels={"a": 0.5, "b": -0.5}),), noise_sigma=0.1),
            ),
        )
    )
    pan = (
        panel
        if panel is not None
        else (
            variable(
                "active",
                VariableKind.BINARY,
                eq=logit(0.0, (term("propensity", 1.0), term("active", 1.0, lagged=True))),
            ),
        )
    )
    population = PopulationSpec(
        name="user",
        size=20,
        attributes=attrs,
        panel=pan,
    )
    return ScenarioSpec(
        name="tiny",
        title="Tiny",
        start=START,
        end=END,
        populations=(population,),
        tables=(
            table("dim_user", "user", TableGrain.ENTITY, (entity_col(), attr_col("segment"))),
            table(
                "fact_user_day",
                "user",
                TableGrain.ENTITY_DAY,
                (entity_col(), date_col(), panel_col("active")),
            ),
        ),
    )


def codes_of(issues: list[LintIssue]) -> set[str]:
    return {issue.code for issue in issues}


class TestLint:
    def test_tiny_spec_is_clean(self) -> None:
        assert require_clean(tiny_spec()) == []

    def test_attribute_cycle_is_reported(self) -> None:
        spec = tiny_spec(
            attributes=(
                variable("a", VariableKind.NUMERIC, eq=eqn(0.0, (term("b", 1.0),))),
                variable("b", VariableKind.NUMERIC, eq=eqn(0.0, (term("a", 1.0),))),
            ),
            panel=(),
        )
        assert "cycle" in codes_of(lint_spec(spec))

    def test_unknown_parent_is_reported(self) -> None:
        spec = tiny_spec(
            attributes=(variable("x", VariableKind.NUMERIC, eq=eqn(0.0, (term("ghost", 1.0),))),),
            panel=(),
        )
        assert "unknown-parent" in codes_of(lint_spec(spec))

    def test_categorical_parent_needs_level_effects(self) -> None:
        spec = tiny_spec(
            attributes=(
                variable(
                    "segment", VariableKind.CATEGORICAL, dist=categorical({"a": 1.0, "b": 1.0})
                ),
                variable("y", VariableKind.NUMERIC, eq=eqn(0.0, (term("segment", 1.0),))),
            ),
            panel=(),
        )
        assert "categorical-term" in codes_of(lint_spec(spec))

    def test_binary_needs_logistic_link(self) -> None:
        spec = tiny_spec(
            attributes=(
                variable("z", VariableKind.NUMERIC, dist=normal(0.0, 1.0)),
                variable(
                    "flag",
                    VariableKind.BINARY,
                    eq=eqn(0.0, (term("z", 1.0),), link=Link.IDENTITY),
                ),
            ),
            panel=(),
        )
        assert "binary-link" in codes_of(lint_spec(spec))

    def test_unknown_table_ref_is_reported(self) -> None:
        base = tiny_spec()
        broken = base.model_copy(
            update={
                "tables": (
                    *base.tables,
                    table(
                        "extra",
                        "user",
                        TableGrain.ENTITY,
                        (entity_col(), attr_col("missing_col")),
                    ),
                )
            }
        )
        assert "unknown-ref" in codes_of(lint_spec(broken))

    def test_volume_guardrail(self) -> None:
        spec = tiny_spec()
        with pytest.raises(SpecLintError, match="guardrail"):
            require_clean(spec, scale=1e9)

    def test_projected_rows(self) -> None:
        spec = tiny_spec()
        # 20 entities + 20 entities * 26 days
        assert projected_rows(spec) == 20 + 20 * spec.n_days


class TestOrders:
    def test_attribute_order_respects_parents(self) -> None:
        spec = tiny_spec()
        order = attribute_order(spec.population("user"))
        assert order.index("segment") < order.index("propensity")

    def test_panel_order_ignores_lagged_self(self) -> None:
        spec = tiny_spec()
        assert panel_order(spec.population("user")) == ("active",)


class TestDeltas:
    def test_apply_deltas_is_idempotent(self) -> None:
        spec = tiny_spec()
        deltas = (
            SpecDelta(
                kind="add_attribute",
                population="user",
                variable=variable(
                    "tier", VariableKind.CATEGORICAL, dist=categorical({"x": 1.0, "y": 2.0})
                ),
            ),
            gap_add_col("dim_user", attr_col("tier")),
            gap_add_view(view("v_tiers", "SELECT tier, count(*) FROM dim_user GROUP BY tier")),
        )
        once = apply_deltas(spec, deltas)
        twice = apply_deltas(once, deltas)
        assert once == twice
        assert require_clean(twice) == []
        assert any(v.name == "tier" for v in twice.population("user").attributes)
        assert any(c.name == "tier" for c in twice.table("dim_user").columns)
        assert any(v.name == "v_tiers" for v in twice.views)

    def test_delta_targeting_unknown_population_fails_lint(self) -> None:
        spec = tiny_spec()
        patched = apply_deltas(
            spec,
            (
                SpecDelta(
                    kind="add_view",
                    view=view("dim_user", "SELECT 1"),  # collides with a table name
                ),
            ),
        )
        assert "view-collision" in codes_of(lint_spec(patched))

    def test_delta_unknown_population_raises(self) -> None:
        spec = tiny_spec()
        delta = SpecDelta(
            kind="add_attribute",
            population="users",  # typo: the population is 'user'
            variable=variable("x", VariableKind.NUMERIC, dist=normal(0.0, 1.0)),
        )
        with pytest.raises(KeyError, match="unknown population"):
            apply_deltas(spec, (delta,))

    def test_delta_unknown_table_raises(self) -> None:
        spec = tiny_spec()
        with pytest.raises(KeyError, match="unknown table"):
            apply_deltas(spec, (gap_add_col("dim_userz", attr_col("segment")),))
