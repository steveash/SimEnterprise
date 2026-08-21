"""The sampling engine: draw tabular data from a linted causal spec at scale.

Evaluation is **vectorized over entities** (numpy) and **streamed over days**,
so memory is bounded by one partition regardless of total volume — the 10GB
run and the unit-test run take the same code path. The pipeline per
population:

1. **Entities** — sample static attributes in topological order (one array per
   attribute, length N), resolving KG identity/dimension bindings first so
   causal terms can condition on real teams/products/people.
2. **Factors** — simulate every global daily series across the window
   (trend + weekday + annual seasonality + AR(1) noise + decaying shocks).
3. **Panel** — for each day, evaluate panel variables in topological order
   against attributes ⊕ factors ⊕ same-day values ⊕ previous-day lags, then
   append the day's rows to the current parquet partition. Partitions close
   when they reach ``rows_per_partition``.

Determinism: every stochastic draw comes from a ``numpy`` generator seeded via
:func:`~enterprise_sim.core.config.seed.derive_subseed` sub-streams keyed by
``(root, purpose, population/factor, …)``, and days are always processed in
order — so chunk boundaries, table layouts, and re-runs never change a single
sampled value. Same seed → byte-identical parquet content.

numpy/pyarrow are imported lazily at module top per the ``bench`` extra
pattern; install ``.[data]`` (or ``--extra dev``) to use this module.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from enterprise_sim.core.config.seed import SeedContext, derive_subseed
from enterprise_sim.core.world import World
from enterprise_sim.data_products import linking
from enterprise_sim.data_products.lint import attribute_order, panel_order
from enterprise_sim.data_products.spec import (
    ColumnSource,
    Distribution,
    DistributionKind,
    FactorSpec,
    LinearPredictor,
    Link,
    NoiseKind,
    NoiseSpec,
    PopulationSpec,
    ScenarioSpec,
    TableGrain,
    TableSpec,
    Transform,
    Variable,
    VariableKind,
)

__all__ = ["SampleReport", "sample_scenario"]

FloatArray = npt.NDArray[np.float64]

# Exponent guard for exp/logistic links: |x| beyond this saturates rather than
# overflowing float64.
_EXP_CLIP = 60.0


@dataclass(frozen=True, slots=True)
class _CatColumn:
    """A categorical column as integer codes over a fixed level vocabulary."""

    codes: npt.NDArray[np.int64]
    levels: tuple[str, ...]

    def as_strings(self) -> npt.NDArray[np.object_]:
        vocab = np.asarray(self.levels, dtype=object)
        result: npt.NDArray[np.object_] = vocab[self.codes]
        return result


_Env = dict[str, Any]  # name -> FloatArray | _CatColumn


def _transform(values: FloatArray, transform: Transform) -> FloatArray:
    if transform is Transform.IDENTITY:
        return values
    if transform is Transform.LOG1P:
        # Domain-guarded: log1p needs x > -1; saturate below.
        return np.log1p(np.maximum(values, -0.999))
    if transform is Transform.SQRT:
        return np.sqrt(np.maximum(values, 0.0))
    if transform is Transform.SQUARE:
        return np.square(values)
    if transform is Transform.ABS:
        return np.abs(values)
    return -values  # NEGATE


def _link(values: FloatArray, link: Link) -> FloatArray:
    if link is Link.IDENTITY:
        return values
    clipped = np.clip(values, -_EXP_CLIP, _EXP_CLIP)
    if link is Link.EXP:
        return np.exp(clipped)
    if link is Link.LOGISTIC:
        return 1.0 / (1.0 + np.exp(-clipped))
    # SOFTPLUS, numerically stable: log1p(exp(-|x|)) + max(x, 0)
    return np.log1p(np.exp(-np.abs(clipped))) + np.maximum(clipped, 0.0)


def _level_effect_vector(
    levels: tuple[str, ...],
    *,
    explicit: Mapping[str, float] | None,
    sigma: float | None,
    seed_root: int,
    scope: str,
) -> FloatArray:
    """Additive effect per level: explicit map, or seeded N(0, sigma) draws.

    Seeded draws are keyed by ``(scope, level name)`` so they are stable across
    chunks, iterations, and re-runs — the effect of "Team Falcon" on churn is a
    fixed (but unknown-to-the-analyst) constant of the scenario.
    """
    effects = np.zeros(len(levels), dtype=np.float64)
    if explicit is not None:
        for index, level in enumerate(levels):
            effects[index] = explicit.get(level, 0.0)
    elif sigma is not None and sigma > 0:
        for index, level in enumerate(levels):
            gen = np.random.default_rng(derive_subseed(seed_root, "level_effect", scope, level))
            effects[index] = gen.standard_normal() * sigma
    return effects


class _PredictorEvaluator:
    """Evaluates linear predictors against an environment of column arrays."""

    def __init__(self, *, seed_root: int, population: str) -> None:
        self._seed_root = seed_root
        self._population = population

    def _parent_values(self, env: _Env, name: str, *, lagged: bool, lags: _Env | None) -> Any:
        source = lags if lagged else env
        if source is None or name not in source:
            raise KeyError(
                f"population {self._population!r}: parent {name!r} "
                f"({'lagged' if lagged else 'same-day'}) missing from environment"
            )
        return source[name]

    def evaluate(
        self,
        predictor: LinearPredictor,
        env: _Env,
        *,
        n: int,
        scope: str,
        lags: _Env | None = None,
    ) -> FloatArray:
        total = np.full(n, predictor.intercept, dtype=np.float64)
        for term in predictor.terms:
            values = self._parent_values(env, term.parent, lagged=term.lagged, lags=lags)
            if isinstance(values, _CatColumn):
                effects = _level_effect_vector(
                    values.levels,
                    explicit=term.level_effects,
                    sigma=term.level_effect_sigma,
                    seed_root=self._seed_root,
                    scope=f"{self._population}/{scope}/{term.parent}",
                )
                total += term.coef * effects[values.codes]
            else:
                total += term.coef * _transform(
                    np.asarray(values, dtype=np.float64), term.transform
                )
        for interaction in predictor.interactions:
            left = self._parent_values(env, interaction.parents[0], lagged=False, lags=None)
            right = self._parent_values(env, interaction.parents[1], lagged=False, lags=None)
            total += interaction.coef * (
                _transform(np.asarray(left, dtype=np.float64), interaction.transforms[0])
                * _transform(np.asarray(right, dtype=np.float64), interaction.transforms[1])
            )
        return total


def _sample_distribution(
    dist: Distribution, *, n: int, gen: np.random.Generator
) -> FloatArray | _CatColumn:
    params = dist.params
    if dist.kind is DistributionKind.NORMAL:
        return gen.normal(params.get("mean", 0.0), params.get("sigma", 1.0), n)
    if dist.kind is DistributionKind.LOGNORMAL:
        return gen.lognormal(params.get("mean", 0.0), params.get("sigma", 1.0), n)
    if dist.kind is DistributionKind.UNIFORM:
        return gen.uniform(params.get("low", 0.0), params.get("high", 1.0), n)
    if dist.kind is DistributionKind.BETA:
        return gen.beta(params.get("alpha", 2.0), params.get("beta", 2.0), n)
    if dist.kind is DistributionKind.GAMMA:
        return gen.gamma(params.get("shape", 2.0), params.get("scale", 1.0), n)
    if dist.kind is DistributionKind.EXPONENTIAL:
        return gen.exponential(params.get("scale", 1.0), n)
    if dist.kind is DistributionKind.POISSON:
        return gen.poisson(max(params.get("lam", 1.0), 0.0), n).astype(np.float64)
    if dist.kind is DistributionKind.BERNOULLI:
        p = min(max(params.get("p", 0.5), 0.0), 1.0)
        return (gen.random(n) < p).astype(np.float64)
    # CATEGORICAL
    weights = np.asarray(dist.weights or [1.0] * len(dist.levels), dtype=np.float64)
    probs = weights / weights.sum()
    codes = gen.choice(len(dist.levels), size=n, p=probs).astype(np.int64)
    return _CatColumn(codes=codes, levels=dist.levels)


def _apply_noise(values: FloatArray, noise: NoiseSpec, gen: np.random.Generator) -> FloatArray:
    if noise.kind is NoiseKind.NORMAL:
        return values + gen.normal(0.0, noise.sigma, values.shape[0])
    if noise.kind is NoiseKind.LOGNORMAL:
        multiplier: FloatArray = np.exp(gen.normal(0.0, noise.sigma, values.shape[0]))
        return values * multiplier
    heavy: FloatArray = values + gen.standard_t(noise.df, values.shape[0]) * noise.sigma
    return heavy  # STUDENT_T


def _realize_variable(
    var: Variable,
    env: _Env,
    *,
    n: int,
    gen: np.random.Generator,
    evaluator: _PredictorEvaluator,
    lags: _Env | None = None,
) -> FloatArray | _CatColumn:
    """Sample one variable's values for ``n`` rows given its parents in ``env``."""
    if var.distribution is not None:
        return _sample_distribution(var.distribution, n=n, gen=gen)

    if var.categorical_equation is not None:
        levels = tuple(var.categorical_equation.utilities)
        utilities = np.empty((len(levels), n), dtype=np.float64)
        for index, level in enumerate(levels):
            predictor = var.categorical_equation.utilities[level]
            utilities[index] = evaluator.evaluate(
                predictor, env, n=n, scope=f"{var.name}[{level}]", lags=lags
            )
        # Gumbel-max: argmax over utility + Gumbel noise == softmax sampling.
        gumbel = -np.log(-np.log(np.clip(gen.random((len(levels), n)), 1e-12, 1.0)))
        codes = np.argmax(utilities + gumbel, axis=0).astype(np.int64)
        return _CatColumn(codes=codes, levels=levels)

    assert var.equation is not None
    eq = var.equation
    values = evaluator.evaluate(eq, env, n=n, scope=var.name, lags=lags)
    values = _link(values, eq.link)
    if eq.noise is not None:
        values = _apply_noise(values, eq.noise, gen)
    if eq.clip_min is not None or eq.clip_max is not None:
        values = np.clip(
            values,
            eq.clip_min if eq.clip_min is not None else -math.inf,
            eq.clip_max if eq.clip_max is not None else math.inf,
        )
    if var.kind is VariableKind.COUNT:
        values = gen.poisson(np.maximum(values, 0.0)).astype(np.float64)
    elif var.kind is VariableKind.BINARY:
        probs = np.clip(values, 0.0, 1.0)
        values = (gen.random(n) < probs).astype(np.float64)
    return values


# -- factors ---------------------------------------------------------------- #


def _simulate_factor(factor: FactorSpec, spec: ScenarioSpec, seed_root: int) -> FloatArray:
    """The factor's daily value over the whole window (index 0 = spec.start)."""
    n_days = spec.n_days
    gen = np.random.default_rng(derive_subseed(seed_root, "factor", factor.name))
    t = np.arange(n_days, dtype=np.float64)
    values = factor.base + factor.trend_per_day * t
    if factor.weekday_effects is not None:
        start_dow = spec.start.weekday()
        dow = (np.arange(n_days) + start_dow) % 7
        values += np.asarray(factor.weekday_effects, dtype=np.float64)[dow]
    if factor.annual_amplitude:
        values += factor.annual_amplitude * np.sin(
            2.0 * np.pi * (t + factor.annual_phase_days) / 365.25
        )
    if factor.noise_sigma > 0:
        shocks_noise = gen.normal(0.0, factor.noise_sigma, n_days)
        state = 0.0
        ar = np.empty(n_days, dtype=np.float64)
        for day in range(n_days):
            state = factor.ar_coef * state + shocks_noise[day]
            ar[day] = state
        values += ar
    for shock in factor.shocks:
        offset = (shock.on - spec.start).days
        if offset >= n_days:
            continue
        start = max(offset, 0)
        decay_t = np.arange(start - offset, n_days - offset, dtype=np.float64)
        values[start:] += shock.magnitude * np.power(shock.decay, decay_t)
    return values


# -- entity sampling -------------------------------------------------------- #


@dataclass(slots=True)
class _Entities:
    """A fully-sampled population: ids, KG bindings, and attribute columns."""

    population: PopulationSpec
    size: int
    entity_ids: npt.NDArray[np.object_]
    kg_ids: tuple[str, ...] | None
    kg_names: tuple[str, ...] | None
    columns: _Env
    dimension_lineage: dict[str, dict[str, str]]  # attribute -> level -> kg node id


def _scaled_size(pop: PopulationSpec, scale: float) -> int:
    return max(1, int(round(pop.size * scale)))


def _sample_entities(
    pop: PopulationSpec,
    spec: ScenarioSpec,
    world: World | None,
    seeds: SeedContext,
    *,
    scale: float,
) -> _Entities:
    kg_ids: tuple[str, ...] | None = None
    kg_names: tuple[str, ...] | None = None
    if pop.kg_identity is not None:
        if world is None:
            raise linking.LinkageError(
                f"population {pop.name!r} has a kg_identity binding but no world was supplied"
            )
        identity = linking.bind_identity(pop.kg_identity, world)
        size = identity.size
        kg_ids, kg_names = identity.kg_ids, identity.kg_names
    else:
        size = _scaled_size(pop, scale)

    width = max(6, len(str(size)))
    entity_ids = np.asarray(
        [f"{pop.name}-{index + 1:0{width}d}" for index in range(size)], dtype=object
    )

    env: _Env = {}
    dimension_lineage: dict[str, dict[str, str]] = {}
    for dim in pop.kg_dimensions:
        if world is None:
            raise linking.LinkageError(
                f"population {pop.name!r} declares kg_dimension {dim.attribute!r} "
                "but no world was supplied"
            )
        binding = linking.assign_dimension(
            dim, world, size=size, rng=seeds.rng("kgdim", pop.name, dim.attribute)
        )
        levels = binding.levels
        index_of = {level: position for position, level in enumerate(levels)}
        codes = np.asarray([index_of[value] for value in binding.values], dtype=np.int64)
        env[dim.attribute] = _CatColumn(codes=codes, levels=levels)
        dimension_lineage[dim.attribute] = dict(binding.level_to_id)

    evaluator = _PredictorEvaluator(seed_root=seeds.root, population=pop.name)
    by_name = {var.name: var for var in pop.attributes}
    for name in attribute_order(pop):
        var = by_name[name]
        gen = np.random.default_rng(derive_subseed(seeds.root, "attr", pop.name, name))
        env[name] = _realize_variable(var, env, n=size, gen=gen, evaluator=evaluator)

    return _Entities(
        population=pop,
        size=size,
        entity_ids=entity_ids,
        kg_ids=kg_ids,
        kg_names=kg_names,
        columns=env,
        dimension_lineage=dimension_lineage,
    )


# -- parquet writing -------------------------------------------------------- #


def _column_arrow(
    values: Any, kind: VariableKind | None, *, dictionary: bool
) -> pa.Array | pa.ChunkedArray:
    """Convert an internal column to an arrow array with a sensible dtype."""
    if isinstance(values, _CatColumn):
        arr = pa.array(values.as_strings(), type=pa.string())
        return arr.dictionary_encode() if dictionary else arr
    data = np.asarray(values)
    if kind is VariableKind.COUNT:
        return pa.array(data.astype(np.int64))
    if kind is VariableKind.BINARY:
        return pa.array(data.astype(np.bool_))
    if data.dtype == object:
        arr = pa.array(data, type=pa.string())
        return arr.dictionary_encode() if dictionary else arr
    return pa.array(data.astype(np.float64))


class _PartitionWriter:
    """Accumulates day batches for one table and flushes sized parquet parts."""

    def __init__(self, out_dir: Path, table: TableSpec, *, rows_per_partition: int) -> None:
        self._dir = out_dir / "tables" / table.name
        self._dir.mkdir(parents=True, exist_ok=True)
        self._table = table
        self._rows_per_partition = rows_per_partition
        self._batches: list[pa.Table] = []
        self._buffered = 0
        self._part_index = 0
        self.rows_written = 0

    def append(self, batch: pa.Table) -> None:
        self._batches.append(batch)
        self._buffered += batch.num_rows
        if self._buffered >= self._rows_per_partition:
            self.flush()

    def flush(self) -> None:
        if not self._batches:
            return
        combined = pa.concat_tables(self._batches)
        path = self._dir / f"part-{self._part_index:05d}.parquet"
        pq.write_table(combined, path)
        self.rows_written += combined.num_rows
        self._part_index += 1
        self._batches = []
        self._buffered = 0


def _entity_table_batch(table: TableSpec, entities: _Entities) -> pa.Table:
    pop = entities.population
    kinds = {var.name: var.kind for var in pop.attributes}
    arrays: list[pa.Array | pa.ChunkedArray] = []
    names: list[str] = []
    for col in table.columns:
        names.append(col.name)
        if col.source is ColumnSource.ENTITY_ID:
            arrays.append(pa.array(entities.entity_ids, type=pa.string()))
        elif col.source is ColumnSource.KG_ID:
            arrays.append(pa.array(list(entities.kg_ids or ()), type=pa.string()))
        elif col.source is ColumnSource.KG_NAME:
            arrays.append(pa.array(list(entities.kg_names or ()), type=pa.string()))
        else:  # ATTRIBUTE
            arrays.append(
                _column_arrow(entities.columns[col.ref], kinds.get(col.ref), dictionary=False)
            )
    return pa.table(dict(zip(names, arrays, strict=True)))


def _panel_day_batch(
    table: TableSpec,
    entities: _Entities,
    day: date,
    env: _Env,
    factor_values: Mapping[str, float],
) -> pa.Table:
    pop = entities.population
    kinds = {var.name: var.kind for var in (*pop.attributes, *pop.panel)}
    n = entities.size
    arrays: list[pa.Array | pa.ChunkedArray] = []
    names: list[str] = []
    for col in table.columns:
        names.append(col.name)
        if col.source is ColumnSource.ENTITY_ID:
            arrays.append(pa.array(entities.entity_ids, type=pa.string()).dictionary_encode())
        elif col.source is ColumnSource.DATE:
            arrays.append(pa.array(np.full(n, day), type=pa.date32()))
        elif col.source is ColumnSource.KG_ID:
            arrays.append(pa.array(list(entities.kg_ids or ()), type=pa.string()))
        elif col.source is ColumnSource.KG_NAME:
            arrays.append(pa.array(list(entities.kg_names or ()), type=pa.string()))
        elif col.source is ColumnSource.FACTOR:
            arrays.append(pa.array(np.full(n, factor_values[col.ref], dtype=np.float64)))
        elif col.source is ColumnSource.ATTRIBUTE:
            arrays.append(
                _column_arrow(entities.columns[col.ref], kinds.get(col.ref), dictionary=True)
            )
        else:  # PANEL
            arrays.append(_column_arrow(env[col.ref], kinds.get(col.ref), dictionary=True))
    return pa.table(dict(zip(names, arrays, strict=True)))


# -- the top-level sample --------------------------------------------------- #


@dataclass(slots=True)
class SampleReport:
    """What one full sampling pass produced (for the manifest + lineage)."""

    rows_by_table: dict[str, int] = field(default_factory=dict)
    population_sizes: dict[str, int] = field(default_factory=dict)
    identity_lineage: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    dimension_lineage: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)


def _numeric_env_view(env: _Env, entities_columns: _Env) -> _Env:
    merged = dict(entities_columns)
    merged.update(env)
    return merged


def sample_scenario(
    spec: ScenarioSpec,
    out_dir: Path,
    *,
    seeds: SeedContext,
    world: World | None = None,
    scale: float = 1.0,
    rows_per_partition: int = 2_000_000,
) -> SampleReport:
    """Sample every table of ``spec`` into ``out_dir/tables/<name>/part-*.parquet``.

    ``seeds`` should already be scoped to the scenario (e.g.
    ``SeedContext(root).child("data", spec.name)``); ``world`` is required when
    any population binds to the KG. The pass is deterministic in
    ``(spec, seeds, world)`` — ``scale`` and ``rows_per_partition`` change
    volume and file layout without re-ordering any draw stream.
    """
    report = SampleReport()
    factor_series = {
        factor.name: _simulate_factor(factor, spec, seeds.root) for factor in spec.factors
    }
    days = [spec.start + timedelta(days=offset) for offset in range(spec.n_days)]

    for pop in spec.populations:
        entities = _sample_entities(pop, spec, world, seeds, scale=scale)
        report.population_sizes[pop.name] = entities.size
        if entities.kg_ids is not None:
            report.identity_lineage[pop.name] = [
                {"entity_id": str(entity), "kg_id": kg_id, "kg_name": kg_name}
                for entity, kg_id, kg_name in zip(
                    entities.entity_ids, entities.kg_ids, entities.kg_names or (), strict=True
                )
            ]
        if entities.dimension_lineage:
            report.dimension_lineage[pop.name] = entities.dimension_lineage

        pop_tables = [table for table in spec.tables if table.population == pop.name]
        entity_tables = [t for t in pop_tables if t.grain is TableGrain.ENTITY]
        panel_tables = [t for t in pop_tables if t.grain is TableGrain.ENTITY_DAY]

        for table in entity_tables:
            writer = _PartitionWriter(out_dir, table, rows_per_partition=rows_per_partition)
            writer.append(_entity_table_batch(table, entities))
            writer.flush()
            report.rows_by_table[table.name] = writer.rows_written

        if not panel_tables or not pop.panel:
            for table in panel_tables:
                # A panel-grain table over a population with no panel variables
                # still gets its id/date/attribute columns, one row per entity-day.
                writer = _PartitionWriter(out_dir, table, rows_per_partition=rows_per_partition)
                for index, day in enumerate(days):
                    factor_values = {name: series[index] for name, series in factor_series.items()}
                    writer.append(_panel_day_batch(table, entities, day, {}, factor_values))
                writer.flush()
                report.rows_by_table[table.name] = writer.rows_written
            continue

        writers = {
            table.name: _PartitionWriter(out_dir, table, rows_per_partition=rows_per_partition)
            for table in panel_tables
        }
        evaluator = _PredictorEvaluator(seed_root=seeds.root, population=pop.name)
        panel_by_name = {var.name: var for var in pop.panel}
        order = panel_order(pop)
        gen = np.random.default_rng(derive_subseed(seeds.root, "panel", pop.name))
        lags: _Env = {name: np.zeros(entities.size, dtype=np.float64) for name in panel_by_name}

        for index, day in enumerate(days):
            factor_values = {name: float(series[index]) for name, series in factor_series.items()}
            env: _Env = {}
            merged = _numeric_env_view(env, entities.columns)
            for name, value in factor_values.items():
                merged[name] = np.full(entities.size, value, dtype=np.float64)
            for name in order:
                var = panel_by_name[name]
                merged[name] = _realize_variable(
                    var, merged, n=entities.size, gen=gen, evaluator=evaluator, lags=lags
                )
                env[name] = merged[name]
            for table in panel_tables:
                writers[table.name].append(
                    _panel_day_batch(table, entities, day, env, factor_values)
                )
            # Next day's lag view: today's realized values as plain numerics.
            for name in panel_by_name:
                value = env[name]
                lags[name] = (
                    value.codes.astype(np.float64) if isinstance(value, _CatColumn) else value
                )

        for table in panel_tables:
            writers[table.name].flush()
            report.rows_by_table[table.name] = writers[table.name].rows_written

    return report
