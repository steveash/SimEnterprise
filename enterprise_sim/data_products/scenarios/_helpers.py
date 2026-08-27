"""Shorthand constructors for hand-authored scenario templates.

The five built-in scenarios declare rich causal graphs; written against the
raw spec models they would drown in keyword noise. These helpers are a thin,
typed layer — every function returns a plain spec model, nothing here adds
semantics — that keeps a template's shape close to how you'd sketch the DAG on
a whiteboard.
"""

from __future__ import annotations

from enterprise_sim.data_products.spec import (
    CategoricalEquation,
    ColumnSource,
    ColumnSpec,
    Distribution,
    DistributionKind,
    GapFix,
    Interaction,
    LinearPredictor,
    Link,
    NoiseKind,
    NoiseSpec,
    NumericEquation,
    QuestionSpec,
    SpecDelta,
    TableGrain,
    TableSpec,
    Term,
    Transform,
    Variable,
    VariableKind,
    ViewSpec,
)

__all__ = [
    "bernoulli",
    "beta",
    "categorical",
    "col",
    "date_col",
    "entity_col",
    "eqn",
    "factor_col",
    "gap",
    "gap_add_attr",
    "gap_add_col",
    "gap_add_panel",
    "gap_add_table",
    "gap_add_view",
    "interact",
    "kg_id_col",
    "kg_name_col",
    "logit",
    "lognormal",
    "normal",
    "question",
    "table",
    "term",
    "uniform",
    "variable",
    "view",
]


# -- distributions ----------------------------------------------------------- #


def normal(mean: float, sigma: float) -> Distribution:
    return Distribution(kind=DistributionKind.NORMAL, params={"mean": mean, "sigma": sigma})


def lognormal(mean: float, sigma: float) -> Distribution:
    return Distribution(kind=DistributionKind.LOGNORMAL, params={"mean": mean, "sigma": sigma})


def uniform(low: float, high: float) -> Distribution:
    return Distribution(kind=DistributionKind.UNIFORM, params={"low": low, "high": high})


def beta(alpha: float, beta_: float) -> Distribution:
    return Distribution(kind=DistributionKind.BETA, params={"alpha": alpha, "beta": beta_})


def bernoulli(p: float) -> Distribution:
    return Distribution(kind=DistributionKind.BERNOULLI, params={"p": p})


def categorical(levels: dict[str, float]) -> Distribution:
    """Categorical distribution from a level → weight map (insertion-ordered)."""
    return Distribution(
        kind=DistributionKind.CATEGORICAL,
        levels=tuple(levels),
        weights=tuple(levels.values()),
    )


# -- equations --------------------------------------------------------------- #


def term(
    parent: str,
    coef: float = 1.0,
    *,
    transform: Transform = Transform.IDENTITY,
    lagged: bool = False,
    levels: dict[str, float] | None = None,
    level_sigma: float | None = None,
) -> Term:
    return Term(
        parent=parent,
        coef=coef,
        transform=transform,
        lagged=lagged,
        level_effects=levels,
        level_effect_sigma=level_sigma,
    )


def interact(left: str, right: str, coef: float) -> Interaction:
    return Interaction(parents=(left, right), coef=coef)


def eqn(
    intercept: float = 0.0,
    terms: tuple[Term, ...] = (),
    *,
    interactions: tuple[Interaction, ...] = (),
    link: Link = Link.IDENTITY,
    noise_sigma: float | None = None,
    noise_kind: NoiseKind = NoiseKind.NORMAL,
    clip_min: float | None = None,
    clip_max: float | None = None,
) -> NumericEquation:
    return NumericEquation(
        intercept=intercept,
        terms=terms,
        interactions=interactions,
        link=link,
        noise=NoiseSpec(kind=noise_kind, sigma=noise_sigma) if noise_sigma else None,
        clip_min=clip_min,
        clip_max=clip_max,
    )


def logit(
    intercept: float,
    terms: tuple[Term, ...],
    *,
    interactions: tuple[Interaction, ...] = (),
) -> NumericEquation:
    """A logistic-link equation (the standard form for binary variables)."""
    return eqn(intercept, terms, interactions=interactions, link=Link.LOGISTIC)


def pred(intercept: float = 0.0, terms: tuple[Term, ...] = ()) -> LinearPredictor:
    """A plain linear predictor (the utility form for categorical equations).

    Categorical utilities must be exactly :class:`LinearPredictor` — an
    equation subclass would not survive the spec's JSON round-trip.
    """
    return LinearPredictor(intercept=intercept, terms=terms)


def variable(
    name: str,
    kind: VariableKind,
    *,
    dist: Distribution | None = None,
    eq: NumericEquation | None = None,
    utilities: dict[str, LinearPredictor] | None = None,
    description: str = "",
    lag_init: float = 0.0,
) -> Variable:
    return Variable(
        name=name,
        description=description,
        kind=kind,
        distribution=dist,
        equation=eq,
        categorical_equation=(
            CategoricalEquation(utilities=utilities) if utilities is not None else None
        ),
        lag_init=lag_init,
    )


# -- tables ------------------------------------------------------------------ #


def entity_col(name: str = "entity_id") -> ColumnSpec:
    return ColumnSpec(name=name, source=ColumnSource.ENTITY_ID)


def date_col(name: str = "date") -> ColumnSpec:
    return ColumnSpec(name=name, source=ColumnSource.DATE)


def kg_id_col(name: str = "kg_id") -> ColumnSpec:
    return ColumnSpec(name=name, source=ColumnSource.KG_ID)


def kg_name_col(name: str = "name") -> ColumnSpec:
    return ColumnSpec(name=name, source=ColumnSource.KG_NAME)


def col(name: str, source: ColumnSource, ref: str | None = None) -> ColumnSpec:
    """A data column; ``ref`` defaults to ``name`` for attribute/panel/factor."""
    if source in (ColumnSource.ATTRIBUTE, ColumnSource.PANEL, ColumnSource.FACTOR):
        return ColumnSpec(name=name, source=source, ref=ref if ref is not None else name)
    return ColumnSpec(name=name, source=source)


def attr_col(name: str, ref: str | None = None) -> ColumnSpec:
    return col(name, ColumnSource.ATTRIBUTE, ref)


def panel_col(name: str, ref: str | None = None) -> ColumnSpec:
    return col(name, ColumnSource.PANEL, ref)


def factor_col(name: str, ref: str | None = None) -> ColumnSpec:
    return col(name, ColumnSource.FACTOR, ref)


def table(
    name: str,
    population: str,
    grain: TableGrain,
    columns: tuple[ColumnSpec, ...],
    *,
    description: str = "",
) -> TableSpec:
    return TableSpec(
        name=name, description=description, population=population, grain=grain, columns=columns
    )


def view(name: str, sql: str, *, description: str = "") -> ViewSpec:
    return ViewSpec(name=name, description=description, sql=sql)


# -- questions & gaps -------------------------------------------------------- #


def question(
    id_: str,
    category: str,
    text: str,
    sql: str,
    *,
    gap_fix: GapFix | None = None,
) -> QuestionSpec:
    return QuestionSpec(id=id_, category=category, question=text, sql=sql, gap=gap_fix)


def gap(description: str, *deltas: SpecDelta) -> GapFix:
    return GapFix(description=description, deltas=tuple(deltas))


def gap_add_attr(population: str, var: Variable) -> SpecDelta:
    return SpecDelta(kind="add_attribute", population=population, variable=var)


def gap_add_panel(population: str, var: Variable) -> SpecDelta:
    return SpecDelta(kind="add_panel_variable", population=population, variable=var)


def gap_add_col(table_name: str, column: ColumnSpec) -> SpecDelta:
    return SpecDelta(kind="add_table_column", table=table_name, column=column)


def gap_add_view(view_spec: ViewSpec) -> SpecDelta:
    return SpecDelta(kind="add_view", view=view_spec)


def gap_add_table(table_spec: TableSpec) -> SpecDelta:
    return SpecDelta(kind="add_table", table_spec=table_spec)
