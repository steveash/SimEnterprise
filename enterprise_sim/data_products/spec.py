"""The causal scenario spec: the typed contract every other module speaks.

A :class:`ScenarioSpec` is the complete, serializable description of one data
scenario — the causal graph (exogenous distributions + structural equations
over categorical and numerical variables), the entity populations it plays out
over, the global daily factors that drive time dynamics, the physical tables
sampled from it, the SQL views materialized over those tables, and the question
bank the iterative analysis loop evaluates.

The spec is pure data (Pydantic v2, frozen, ``extra="forbid"``) so it can be

* hand-authored by a scenario template plugin (the deterministic skeleton),
* generated or extended by an LLM through ``generate_structured`` against
  ``ScenarioSpec.model_json_schema()``, and
* snapshotted verbatim into a run's output as the **causal ground truth** the
  sampled data was drawn from (the structured-data analog of the gold KG).

Semantics that matter to the sampler (see ``sampler.py``):

* A **population** is a set of entities (users, accounts, tickets). Its
  ``attributes`` are sampled once per entity; its ``panel`` variables are
  sampled per entity per day.
* **Factors** are global daily latent series (market demand, release quality)
  shared by every entity — the cross-entity correlation channel.
* An endogenous variable's equation is evaluated in causal (topological)
  order; parents may be attributes, KG dimensions, factors, same-day panel
  variables, or (``lagged=True``) the previous day's panel values.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


class _FrozenSpec(BaseModel):
    """Base for spec models: immutable, strict, and typo-proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# -- exogenous distributions ------------------------------------------------ #


class DistributionKind(StrEnum):
    """Supported exogenous sampling distributions."""

    NORMAL = "normal"  # params: mean, sigma
    LOGNORMAL = "lognormal"  # params: mean, sigma (of underlying normal)
    UNIFORM = "uniform"  # params: low, high
    BETA = "beta"  # params: alpha, beta
    GAMMA = "gamma"  # params: shape, scale
    EXPONENTIAL = "exponential"  # params: scale
    POISSON = "poisson"  # params: lam
    BERNOULLI = "bernoulli"  # params: p
    CATEGORICAL = "categorical"  # levels + weights


class Distribution(_FrozenSpec):
    """An exogenous sampling distribution for a root (parentless) variable."""

    kind: DistributionKind
    params: dict[str, float] = Field(
        default_factory=dict,
        description="Numeric parameters keyed by name (see DistributionKind comments).",
    )
    levels: tuple[str, ...] = Field(
        default=(),
        description="Category labels (categorical only).",
    )
    weights: tuple[float, ...] = Field(
        default=(),
        description="Unnormalized sampling weights, parallel to `levels`; empty = uniform.",
    )

    @model_validator(mode="after")
    def _check(self) -> Distribution:
        if self.kind is DistributionKind.CATEGORICAL:
            if len(self.levels) < 2:
                raise ValueError("categorical distribution needs at least 2 levels")
            if self.weights and len(self.weights) != len(self.levels):
                raise ValueError("categorical weights must be parallel to levels")
            if any(w < 0 for w in self.weights):
                raise ValueError("categorical weights must be non-negative")
        elif self.levels or self.weights:
            raise ValueError(f"levels/weights are only valid for categorical, not {self.kind}")
        return self


# -- structural equations --------------------------------------------------- #


class Transform(StrEnum):
    """Elementwise transform applied to a numeric parent before its coefficient."""

    IDENTITY = "identity"
    LOG1P = "log1p"
    SQRT = "sqrt"
    SQUARE = "square"
    ABS = "abs"
    NEGATE = "negate"


class Link(StrEnum):
    """Link function applied to a linear predictor's output."""

    IDENTITY = "identity"
    EXP = "exp"
    LOGISTIC = "logistic"
    SOFTPLUS = "softplus"


class NoiseKind(StrEnum):
    """Additive/multiplicative noise attached to a numeric equation."""

    NORMAL = "normal"  # additive N(0, sigma)
    LOGNORMAL = "lognormal"  # multiplicative exp(N(0, sigma))
    STUDENT_T = "student_t"  # additive heavy-tailed t(df) * sigma


class NoiseSpec(_FrozenSpec):
    """Noise term for a numeric structural equation."""

    kind: NoiseKind = NoiseKind.NORMAL
    sigma: float = Field(gt=0.0)
    df: float = Field(default=5.0, gt=2.0, description="Degrees of freedom (student_t only).")


class Term(_FrozenSpec):
    """One parent's contribution to a linear predictor.

    Numeric parents contribute ``coef * transform(parent)``. Categorical parents
    contribute a per-level effect: either an explicit ``level_effects`` map, or —
    when levels are only known at sample time (KG-bound dimensions) —
    ``level_effect_sigma`` draws a deterministic per-level effect from a seeded
    N(0, sigma) sub-stream keyed by the level's name.
    """

    parent: str = Field(min_length=1)
    coef: float = 1.0
    transform: Transform = Transform.IDENTITY
    lagged: bool = Field(
        default=False,
        description="Read the parent's previous-day value (panel variables only).",
    )
    level_effects: dict[str, float] | None = Field(
        default=None,
        description="Categorical parents: additive effect per level (missing levels = 0).",
    )
    level_effect_sigma: float | None = Field(
        default=None,
        ge=0.0,
        description="Categorical parents: draw per-level effects from seeded N(0, sigma).",
    )

    @model_validator(mode="after")
    def _check(self) -> Term:
        if self.level_effects is not None and self.level_effect_sigma is not None:
            raise ValueError("set level_effects or level_effect_sigma, not both")
        return self


class Interaction(_FrozenSpec):
    """A pairwise product term between two *numeric* parents."""

    parents: tuple[str, str]
    coef: float
    transforms: tuple[Transform, Transform] = (Transform.IDENTITY, Transform.IDENTITY)

    @model_validator(mode="after")
    def _check(self) -> Interaction:
        if self.parents[0] == self.parents[1]:
            raise ValueError("interaction parents must be distinct (use transform=square)")
        return self


class LinearPredictor(_FrozenSpec):
    """``intercept + Σ terms + Σ interactions`` — the shared equation core."""

    intercept: float = 0.0
    terms: tuple[Term, ...] = ()
    interactions: tuple[Interaction, ...] = ()


class NumericEquation(LinearPredictor):
    """Structural equation for a numeric / count / binary endogenous variable.

    Evaluation: ``value = clip(link(predictor) ⊕ noise)`` where ⊕ is additive or
    multiplicative per :class:`NoiseKind`. The variable's ``kind`` decides the
    final realization (see :class:`Variable`).
    """

    link: Link = Link.IDENTITY
    noise: NoiseSpec | None = None
    clip_min: float | None = None
    clip_max: float | None = None

    @model_validator(mode="after")
    def _check_clip(self) -> NumericEquation:
        if self.clip_min is not None and self.clip_max is not None:
            if self.clip_max < self.clip_min:
                raise ValueError("clip_max must be >= clip_min")
        return self


class CategoricalEquation(_FrozenSpec):
    """Multinomial-logit structural equation for a categorical endogenous variable.

    Each level gets a linear-predictor utility; the sampled level is drawn from
    ``softmax(utilities)`` per row.
    """

    utilities: dict[str, LinearPredictor] = Field(min_length=2)


class VariableKind(StrEnum):
    """The realized type of a variable's values."""

    NUMERIC = "numeric"
    COUNT = "count"
    BINARY = "binary"
    CATEGORICAL = "categorical"


class Variable(_FrozenSpec):
    """One node of the causal graph.

    Exactly one of ``distribution`` (exogenous root), ``equation`` (numeric /
    count / binary endogenous), or ``categorical_equation`` (categorical
    endogenous) must be set. Realization by ``kind``:

    * ``numeric`` — the equation value itself (noise/clip per the equation).
    * ``count`` — ``Poisson(rate)`` with the equation value as the rate
      (clipped at 0), so the link should keep it non-negative.
    * ``binary`` — ``Bernoulli(p)`` with the equation value as ``p``
      (the lint requires a logistic link so p is a probability).
    * ``categorical`` — softmax draw over the categorical equation's utilities.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    kind: VariableKind
    distribution: Distribution | None = None
    equation: NumericEquation | None = None
    categorical_equation: CategoricalEquation | None = None

    @model_validator(mode="after")
    def _check(self) -> Variable:
        set_count = sum(
            spec is not None
            for spec in (self.distribution, self.equation, self.categorical_equation)
        )
        if set_count != 1:
            raise ValueError(
                f"variable {self.name!r} must set exactly one of "
                "distribution / equation / categorical_equation"
            )
        if self.categorical_equation is not None and self.kind is not VariableKind.CATEGORICAL:
            raise ValueError(f"variable {self.name!r}: categorical_equation requires kind")
        if self.kind is VariableKind.CATEGORICAL:
            if self.equation is not None:
                raise ValueError(
                    f"variable {self.name!r}: categorical variables use "
                    "distribution or categorical_equation, not equation"
                )
            if (
                self.distribution is not None
                and self.distribution.kind is not DistributionKind.CATEGORICAL
            ):
                raise ValueError(
                    f"variable {self.name!r}: categorical kind needs a categorical distribution"
                )
        return self

    def levels(self) -> tuple[str, ...]:
        """The category labels of a categorical variable ('' for other kinds)."""
        if self.distribution is not None and self.distribution.levels:
            return self.distribution.levels
        if self.categorical_equation is not None:
            return tuple(self.categorical_equation.utilities)
        return ()


# -- global daily factors --------------------------------------------------- #


class Shock(_FrozenSpec):
    """A dated impulse to a factor that decays geometrically per day."""

    on: date
    magnitude: float
    decay: float = Field(default=0.8, gt=0.0, le=1.0)
    description: str = ""


class FactorSpec(_FrozenSpec):
    """A global daily latent series shared by every entity.

    ``value(t) = base + trend_per_day*t + weekday_effects[dow(t)]
    + annual_amplitude * sin(2π (t + annual_phase_days) / 365.25)
    + AR(1) noise state + Σ shock effects``.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    base: float = 0.0
    trend_per_day: float = 0.0
    weekday_effects: tuple[float, float, float, float, float, float, float] | None = Field(
        default=None, description="Additive effect per weekday, Monday..Sunday."
    )
    annual_amplitude: float = 0.0
    annual_phase_days: float = 0.0
    noise_sigma: float = Field(default=0.0, ge=0.0)
    ar_coef: float = Field(default=0.0, ge=0.0, lt=1.0)
    shocks: tuple[Shock, ...] = ()


# -- populations & KG binding ----------------------------------------------- #


class KgIdentity(_FrozenSpec):
    """Bind a population 1:1 to KG nodes: one entity row per matching node.

    The population's size becomes the number of matching nodes; every row gets
    ``kg_id`` and ``kg_name`` pseudo-attributes, and the mapping is recorded in
    ``lineage.json``.
    """

    node_type: str = Field(min_length=1, description="KG node type, e.g. 'Person', 'Team'.")
    where: dict[str, str] = Field(
        default_factory=dict, description="Exact-match filters on node props."
    )


class KgDimension(_FrozenSpec):
    """A categorical attribute whose levels are drawn from KG nodes.

    Each entity is assigned one matching KG node (a team, a product/project, a
    region); the attribute value is the node's display name and the name→id
    mapping is recorded in ``lineage.json``. Zipf-ish popularity weighting makes
    big levels bigger, like real dimensions.
    """

    attribute: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    node_type: str = Field(min_length=1)
    where: dict[str, str] = Field(default_factory=dict)
    weighting: Literal["uniform", "zipf"] = "zipf"
    description: str = ""


class PopulationSpec(_FrozenSpec):
    """A set of entities: static attributes plus an optional per-day panel."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    size: int = Field(gt=0, description="Base entity count (scaled by config; see ScaleConfig).")
    kg_identity: KgIdentity | None = None
    kg_dimensions: tuple[KgDimension, ...] = ()
    attributes: tuple[Variable, ...] = ()
    panel: tuple[Variable, ...] = ()


# -- physical tables & views ------------------------------------------------ #


class ColumnSource(StrEnum):
    """What a table column is filled from."""

    ENTITY_ID = "entity_id"  # the synthetic stable entity id
    DATE = "date"  # the panel date (entity_day grain only)
    KG_ID = "kg_id"  # KG node id (identity-bound populations)
    KG_NAME = "kg_name"  # KG node display name (identity-bound populations)
    ATTRIBUTE = "attribute"  # a static attribute or KG dimension
    PANEL = "panel"  # a panel variable (entity_day grain only)
    FACTOR = "factor"  # a global factor's daily value (entity_day grain only)


class ColumnSpec(_FrozenSpec):
    """One physical column: an output name and the spec variable behind it."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    source: ColumnSource
    ref: str = Field(
        default="",
        description="Referenced variable/attribute/factor name (required for those sources).",
    )
    description: str = ""

    @model_validator(mode="after")
    def _check(self) -> ColumnSpec:
        needs_ref = self.source in (ColumnSource.ATTRIBUTE, ColumnSource.PANEL, ColumnSource.FACTOR)
        if needs_ref and not self.ref:
            raise ValueError(f"column {self.name!r}: source {self.source} requires a ref")
        if not needs_ref and self.ref:
            raise ValueError(f"column {self.name!r}: source {self.source} takes no ref")
        return self


class TableGrain(StrEnum):
    """The physical grain of a table."""

    ENTITY = "entity"  # one row per entity (a dimension table)
    ENTITY_DAY = "entity_day"  # one row per entity per day (a fact table)


class TableSpec(_FrozenSpec):
    """A physical parquet table sampled from one population."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    population: str = Field(min_length=1)
    grain: TableGrain
    columns: tuple[ColumnSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> TableSpec:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"table {self.name!r} has duplicate column names")
        if self.grain is TableGrain.ENTITY:
            for col in self.columns:
                if col.source in (ColumnSource.DATE, ColumnSource.PANEL, ColumnSource.FACTOR):
                    raise ValueError(
                        f"table {self.name!r} column {col.name!r}: "
                        f"source {col.source} needs entity_day grain"
                    )
        return self


class ViewSpec(_FrozenSpec):
    """A materialized view: DuckDB SQL over the scenario's tables and earlier views."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    sql: str = Field(min_length=1)


# -- questions -------------------------------------------------------------- #


class SpecDelta(_FrozenSpec):
    """One additive change to a spec, proposed to close an answerability gap."""

    kind: Literal[
        "add_attribute",
        "add_panel_variable",
        "add_kg_dimension",
        "add_factor",
        "add_table",
        "add_table_column",
        "add_view",
    ]
    population: str | None = Field(
        default=None, description="Target population (variable/dimension deltas)."
    )
    table: str | None = Field(default=None, description="Target table (add_table_column).")
    variable: Variable | None = None
    kg_dimension: KgDimension | None = None
    factor: FactorSpec | None = None
    table_spec: TableSpec | None = None
    column: ColumnSpec | None = None
    view: ViewSpec | None = None

    @model_validator(mode="after")
    def _check(self) -> SpecDelta:
        required: dict[str, tuple[object, ...]] = {
            "add_attribute": (self.population, self.variable),
            "add_panel_variable": (self.population, self.variable),
            "add_kg_dimension": (self.population, self.kg_dimension),
            "add_factor": (self.factor,),
            "add_table": (self.table_spec,),
            "add_table_column": (self.table, self.column),
            "add_view": (self.view,),
        }
        if any(part is None for part in required[self.kind]):
            raise ValueError(f"delta kind {self.kind!r} is missing its required fields")
        return self


class GapFix(_FrozenSpec):
    """How to change the data so a currently-unanswerable question becomes answerable."""

    description: str = ""
    deltas: tuple[SpecDelta, ...] = Field(min_length=1)


class QuestionSpec(_FrozenSpec):
    """One business question with the SQL that answers it.

    ``gap`` is the *known* fix when the question is expected to be unanswerable
    against the initial spec (template question banks encode the gap loop this
    way); LLM-mode gap analysis proposes fixes for questions without one.
    """

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    category: str = Field(min_length=1)
    question: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    gap: GapFix | None = None


# -- the scenario root ------------------------------------------------------ #


class ScenarioSpec(_FrozenSpec):
    """The complete causal description of one data scenario."""

    schema_version: int = SCHEMA_VERSION
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    description: str = ""
    start: date
    end: date
    factors: tuple[FactorSpec, ...] = ()
    populations: tuple[PopulationSpec, ...] = Field(min_length=1)
    tables: tuple[TableSpec, ...] = Field(min_length=1)
    views: tuple[ViewSpec, ...] = ()
    question_categories: tuple[str, ...] = ()
    questions: tuple[QuestionSpec, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> ScenarioSpec:
        if self.end < self.start:
            raise ValueError(f"end ({self.end}) must not precede start ({self.start})")
        return self

    @property
    def n_days(self) -> int:
        """Number of days in the scenario window (inclusive)."""
        return (self.end - self.start).days + 1

    def population(self, name: str) -> PopulationSpec:
        """Return the population named ``name`` (raises ``KeyError`` if absent)."""
        for pop in self.populations:
            if pop.name == name:
                return pop
        raise KeyError(f"no population named {name!r}")

    def table(self, name: str) -> TableSpec:
        """Return the table named ``name`` (raises ``KeyError`` if absent)."""
        for tbl in self.tables:
            if tbl.name == name:
                return tbl
        raise KeyError(f"no table named {name!r}")


def apply_deltas(spec: ScenarioSpec, deltas: tuple[SpecDelta, ...]) -> ScenarioSpec:
    """Return a new spec with ``deltas`` applied (idempotent for repeats).

    Additions that already exist (same name in the same scope) are skipped
    rather than duplicated, so re-applying a gap fix across loop iterations is
    harmless. The result still needs a lint pass — deltas can reference parents
    or populations that don't exist.
    """
    data = spec.model_dump(mode="python")
    for delta in deltas:
        if delta.kind in ("add_attribute", "add_panel_variable", "add_kg_dimension"):
            for pop in data["populations"]:
                if pop["name"] != delta.population:
                    continue
                if delta.kind == "add_kg_dimension":
                    assert delta.kg_dimension is not None
                    existing = {d["attribute"] for d in pop["kg_dimensions"]}
                    if delta.kg_dimension.attribute not in existing:
                        pop["kg_dimensions"] = [
                            *pop["kg_dimensions"],
                            delta.kg_dimension.model_dump(mode="python"),
                        ]
                else:
                    assert delta.variable is not None
                    key = "attributes" if delta.kind == "add_attribute" else "panel"
                    existing = {v["name"] for v in pop[key]}
                    if delta.variable.name not in existing:
                        pop[key] = [*pop[key], delta.variable.model_dump(mode="python")]
        elif delta.kind == "add_factor":
            assert delta.factor is not None
            if delta.factor.name not in {f["name"] for f in data["factors"]}:
                data["factors"] = [*data["factors"], delta.factor.model_dump(mode="python")]
        elif delta.kind == "add_table":
            assert delta.table_spec is not None
            if delta.table_spec.name not in {t["name"] for t in data["tables"]}:
                data["tables"] = [*data["tables"], delta.table_spec.model_dump(mode="python")]
        elif delta.kind == "add_table_column":
            assert delta.column is not None
            for tbl in data["tables"]:
                if tbl["name"] != delta.table:
                    continue
                if delta.column.name not in {c["name"] for c in tbl["columns"]}:
                    tbl["columns"] = [*tbl["columns"], delta.column.model_dump(mode="python")]
        elif delta.kind == "add_view":
            assert delta.view is not None
            if delta.view.name not in {v["name"] for v in data["views"]}:
                data["views"] = [*data["views"], delta.view.model_dump(mode="python")]
    return ScenarioSpec.model_validate(data)
