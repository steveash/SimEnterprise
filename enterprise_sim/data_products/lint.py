"""Spec lint: everything Pydantic's per-model validation cannot see.

Pydantic guarantees each spec node is well-formed in isolation; this module
checks the *graph*: parent references resolve to the right namespace, the
causal graph is acyclic (lagged edges are the one legal back-edge), links match
variable kinds, tables/views/questions reference real things, and the projected
row volume is sane. The sampler trusts a linted spec, so the topological
orders computed here are exported for it to execute in.

Mirrors the authoring quality stack's tiering (ARCHITECTURE.md §13 Tier 1):
static, no execution, fast enough to run on every authored or LLM-proposed
spec before any sampling cost is incurred.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from enterprise_sim.data_products.spec import (
    ColumnSource,
    DistributionKind,
    LinearPredictor,
    Link,
    PopulationSpec,
    ScenarioSpec,
    TableGrain,
    Variable,
    VariableKind,
)

__all__ = [
    "LintIssue",
    "SpecLintError",
    "attribute_order",
    "lint_spec",
    "panel_order",
    "require_clean",
]

# Row-volume guardrail: over this many projected rows the lint errors out so a
# typo'd population size cannot melt a laptop. Config-scaled sizes are checked
# by the runner against the same limit.
MAX_PROJECTED_ROWS = 2_000_000_000


@dataclass(frozen=True, slots=True)
class LintIssue:
    """One lint finding. ``severity`` is ``"error"`` or ``"warning"``."""

    severity: str
    code: str
    message: str


class SpecLintError(Exception):
    """Raised by :func:`require_clean` when a spec has lint errors."""

    def __init__(self, issues: list[LintIssue]) -> None:
        errors = [issue for issue in issues if issue.severity == "error"]
        summary = "; ".join(f"[{issue.code}] {issue.message}" for issue in errors[:10])
        if len(errors) > 10:
            summary += f" (+{len(errors) - 10} more)"
        super().__init__(f"spec has {len(errors)} lint error(s): {summary}")
        self.issues = issues


def _predictors_of(var: Variable) -> Iterable[LinearPredictor]:
    if var.equation is not None:
        yield var.equation
    if var.categorical_equation is not None:
        yield from var.categorical_equation.utilities.values()


def _parents_of(var: Variable) -> set[tuple[str, bool]]:
    """All (parent, lagged) references of a variable's equations."""
    parents: set[tuple[str, bool]] = set()
    for predictor in _predictors_of(var):
        for term in predictor.terms:
            parents.add((term.parent, term.lagged))
        for interaction in predictor.interactions:
            for parent in interaction.parents:
                parents.add((parent, False))
    return parents


def _is_categorical(name: str, pop: PopulationSpec, panel_vars: Mapping[str, Variable]) -> bool:
    if name in {dim.attribute for dim in pop.kg_dimensions}:
        return True
    for var in (*pop.attributes, *pop.panel):
        if var.name == name:
            return var.kind is VariableKind.CATEGORICAL
    return name in panel_vars and panel_vars[name].kind is VariableKind.CATEGORICAL


def _toposort(
    nodes: Iterable[str], edges: Mapping[str, set[str]], *, scope: str, issues: list[LintIssue]
) -> tuple[str, ...]:
    """Topologically order ``nodes`` given parent ``edges``; report cycles.

    ``nodes`` must arrive in a deterministic (declaration) order — the sorter
    breaks ties by insertion order, and the sampler consumes its RNG streams in
    this order, so a set here would silently break run reproducibility.
    """
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for node in nodes:
        sorter.add(node, *sorted(edges.get(node, set())))
    try:
        return tuple(sorter.static_order())
    except CycleError as exc:
        cycle = exc.args[1] if len(exc.args) > 1 else ()
        issues.append(
            LintIssue(
                "error",
                "cycle",
                f"{scope}: causal cycle through {' -> '.join(str(n) for n in cycle)}",
            )
        )
        return tuple(nodes)


def attribute_order(pop: PopulationSpec) -> tuple[str, ...]:
    """Topological evaluation order of a population's static attributes."""
    attr_names = [var.name for var in pop.attributes]
    known = set(attr_names)
    edges: dict[str, set[str]] = {}
    for var in pop.attributes:
        edges[var.name] = {parent for parent, _ in _parents_of(var) if parent in known}
    issues: list[LintIssue] = []
    order = _toposort(attr_names, edges, scope=f"population {pop.name!r} attributes", issues=issues)
    if issues:
        raise SpecLintError(issues)
    return order


def panel_order(pop: PopulationSpec) -> tuple[str, ...]:
    """Topological evaluation order of a population's panel variables.

    Only same-day (non-lagged) panel-to-panel edges constrain the order —
    lagged reads see the previous day's already-computed values.
    """
    panel_names = [var.name for var in pop.panel]
    known = set(panel_names)
    edges: dict[str, set[str]] = {}
    for var in pop.panel:
        edges[var.name] = {
            parent for parent, lagged in _parents_of(var) if parent in known and not lagged
        }
    issues: list[LintIssue] = []
    order = _toposort(panel_names, edges, scope=f"population {pop.name!r} panel", issues=issues)
    if issues:
        raise SpecLintError(issues)
    return order


def _lint_predictor(
    var: Variable,
    pop: PopulationSpec,
    *,
    numeric_parents: set[str],
    categorical_parents: set[str],
    lagged_allowed: set[str],
    scope: str,
    issues: list[LintIssue],
) -> None:
    known = numeric_parents | categorical_parents
    for predictor in _predictors_of(var):
        for term in predictor.terms:
            where = f"{scope}: variable {var.name!r} term on {term.parent!r}"
            if term.parent not in known:
                issues.append(
                    LintIssue("error", "unknown-parent", f"{where} is not a known parent")
                )
                continue
            if term.lagged and term.parent not in lagged_allowed:
                issues.append(
                    LintIssue(
                        "error",
                        "bad-lag",
                        f"{where} is lagged but only panel variables can be lagged",
                    )
                )
            is_cat = term.parent in categorical_parents
            has_level_effects = (
                term.level_effects is not None or term.level_effect_sigma is not None
            )
            if is_cat and not has_level_effects:
                issues.append(
                    LintIssue(
                        "error",
                        "categorical-term",
                        f"{where}: categorical parent needs level_effects or level_effect_sigma",
                    )
                )
            if not is_cat and has_level_effects:
                issues.append(
                    LintIssue(
                        "error",
                        "numeric-term",
                        f"{where}: level effects are only valid for categorical parents",
                    )
                )
        for interaction in predictor.interactions:
            for parent in interaction.parents:
                where = f"{scope}: variable {var.name!r} interaction on {parent!r}"
                if parent not in known:
                    issues.append(
                        LintIssue("error", "unknown-parent", f"{where} is not a known parent")
                    )
                elif parent in categorical_parents:
                    issues.append(
                        LintIssue(
                            "error",
                            "categorical-interaction",
                            f"{where}: interactions require numeric parents",
                        )
                    )


def _lint_variable_kind(var: Variable, *, scope: str, issues: list[LintIssue]) -> None:
    where = f"{scope}: variable {var.name!r}"
    if var.kind is VariableKind.BINARY and var.equation is not None:
        if var.equation.link is not Link.LOGISTIC:
            issues.append(
                LintIssue("error", "binary-link", f"{where}: binary equations need link='logistic'")
            )
    if var.kind is VariableKind.COUNT and var.equation is not None:
        nonneg = var.equation.link in (Link.EXP, Link.SOFTPLUS, Link.LOGISTIC) or (
            var.equation.clip_min is not None and var.equation.clip_min >= 0
        )
        if not nonneg:
            issues.append(
                LintIssue(
                    "warning",
                    "count-rate",
                    f"{where}: count rate can go negative; use exp/softplus link or clip_min=0 "
                    "(negative rates are clipped to 0 at sample time)",
                )
            )
    if var.kind is VariableKind.BINARY and var.distribution is not None:
        if var.distribution.kind is not DistributionKind.BERNOULLI:
            issues.append(
                LintIssue(
                    "error",
                    "binary-dist",
                    f"{where}: exogenous binary variables need a bernoulli distribution",
                )
            )
    if var.kind is VariableKind.NUMERIC and var.distribution is not None:
        if var.distribution.kind in (DistributionKind.CATEGORICAL,):
            issues.append(
                LintIssue(
                    "error",
                    "numeric-dist",
                    f"{where}: numeric variables cannot use a categorical distribution",
                )
            )


def _lint_population(pop: PopulationSpec, spec: ScenarioSpec, issues: list[LintIssue]) -> None:
    scope = f"population {pop.name!r}"
    kg_dim_names = [dim.attribute for dim in pop.kg_dimensions]
    attr_names = [var.name for var in pop.attributes]
    panel_names = [var.name for var in pop.panel]
    factor_names = [factor.name for factor in spec.factors]

    all_names = [*kg_dim_names, *attr_names, *panel_names]
    seen: set[str] = set()
    for name in all_names:
        if name in seen:
            issues.append(
                LintIssue("error", "duplicate-name", f"{scope}: duplicate variable name {name!r}")
            )
        seen.add(name)
    for name in all_names:
        if name in factor_names:
            issues.append(
                LintIssue(
                    "error",
                    "factor-collision",
                    f"{scope}: variable {name!r} collides with a global factor name",
                )
            )
    reserved = {"entity_id", "date", "kg_id", "kg_name"}
    for name in all_names:
        if name in reserved:
            issues.append(
                LintIssue("error", "reserved-name", f"{scope}: {name!r} is a reserved column name")
            )

    panel_vars = {var.name: var for var in pop.panel}
    attr_categorical = {
        name for name in (*kg_dim_names, *attr_names) if _is_categorical(name, pop, panel_vars)
    }

    # Static attributes: parents are earlier attributes + kg dimensions only.
    attr_known_numeric = {
        var.name for var in pop.attributes if var.kind is not VariableKind.CATEGORICAL
    }
    for var in pop.attributes:
        _lint_predictor(
            var,
            pop,
            numeric_parents=attr_known_numeric,
            categorical_parents=attr_categorical,
            lagged_allowed=set(),
            scope=scope,
            issues=issues,
        )
        _lint_variable_kind(var, scope=scope, issues=issues)
        for parent, _lagged in _parents_of(var):
            if parent in panel_names:
                issues.append(
                    LintIssue(
                        "error",
                        "attr-panel-parent",
                        f"{scope}: attribute {var.name!r} cannot depend on panel "
                        f"variable {parent!r}",
                    )
                )
            elif parent in factor_names:
                issues.append(
                    LintIssue(
                        "error",
                        "attr-factor-parent",
                        f"{scope}: attribute {var.name!r} cannot depend on factor {parent!r} "
                        "(factors vary by day; attributes are static)",
                    )
                )

    # Panel variables: parents are attributes, kg dims, factors, panel vars.
    panel_numeric = {var.name for var in pop.panel if var.kind is not VariableKind.CATEGORICAL}
    panel_categorical = {var.name for var in pop.panel if var.kind is VariableKind.CATEGORICAL}
    for var in pop.panel:
        _lint_predictor(
            var,
            pop,
            numeric_parents=attr_known_numeric | set(factor_names) | panel_numeric,
            categorical_parents=attr_categorical | panel_categorical,
            lagged_allowed=set(panel_names),
            scope=scope,
            issues=issues,
        )
        _lint_variable_kind(var, scope=scope, issues=issues)

    # Cycle detection (records issues instead of raising).
    attr_edges = {
        var.name: {p for p, _ in _parents_of(var) if p in set(attr_names)} for var in pop.attributes
    }
    _toposort(attr_names, attr_edges, scope=f"{scope} attributes", issues=issues)
    panel_edges = {
        var.name: {p for p, lagged in _parents_of(var) if p in set(panel_names) and not lagged}
        for var in pop.panel
    }
    _toposort(panel_names, panel_edges, scope=f"{scope} panel", issues=issues)


def _lint_tables(spec: ScenarioSpec, issues: list[LintIssue]) -> None:
    table_names = [tbl.name for tbl in spec.tables]
    if len(set(table_names)) != len(table_names):
        issues.append(LintIssue("error", "duplicate-table", "duplicate table names"))
    pop_names = {pop.name for pop in spec.populations}
    factor_names = {factor.name for factor in spec.factors}
    for tbl in spec.tables:
        scope = f"table {tbl.name!r}"
        if tbl.population not in pop_names:
            issues.append(
                LintIssue(
                    "error", "unknown-population", f"{scope}: unknown population {tbl.population!r}"
                )
            )
            continue
        pop = spec.population(tbl.population)
        attr_like = {var.name for var in pop.attributes} | {
            dim.attribute for dim in pop.kg_dimensions
        }
        panel_names = {var.name for var in pop.panel}
        for col in tbl.columns:
            where = f"{scope}: column {col.name!r}"
            if col.source is ColumnSource.ATTRIBUTE and col.ref not in attr_like:
                issues.append(
                    LintIssue("error", "unknown-ref", f"{where} references unknown attribute")
                )
            elif col.source is ColumnSource.PANEL:
                if col.ref not in panel_names:
                    issues.append(
                        LintIssue(
                            "error", "unknown-ref", f"{where} references unknown panel variable"
                        )
                    )
                if tbl.grain is not TableGrain.ENTITY_DAY:
                    issues.append(
                        LintIssue("error", "grain-mismatch", f"{where} needs entity_day grain")
                    )
            elif col.source is ColumnSource.FACTOR and col.ref not in factor_names:
                issues.append(
                    LintIssue("error", "unknown-ref", f"{where} references unknown factor")
                )
            elif col.source in (ColumnSource.KG_ID, ColumnSource.KG_NAME):
                if pop.kg_identity is None:
                    issues.append(
                        LintIssue(
                            "error",
                            "no-kg-identity",
                            f"{where}: population {pop.name!r} has no kg_identity binding",
                        )
                    )


def _lint_views_and_questions(spec: ScenarioSpec, issues: list[LintIssue]) -> None:
    table_names = {tbl.name for tbl in spec.tables}
    seen_views: set[str] = set()
    for view in spec.views:
        if view.name in table_names:
            issues.append(
                LintIssue(
                    "error", "view-collision", f"view {view.name!r} collides with a table name"
                )
            )
        if view.name in seen_views:
            issues.append(LintIssue("error", "duplicate-view", f"duplicate view {view.name!r}"))
        seen_views.add(view.name)

    seen_questions: set[str] = set()
    categories = set(spec.question_categories)
    pop_names = {pop.name for pop in spec.populations}
    for question in spec.questions:
        if question.id in seen_questions:
            issues.append(
                LintIssue("error", "duplicate-question", f"duplicate question id {question.id!r}")
            )
        seen_questions.add(question.id)
        if categories and question.category not in categories:
            issues.append(
                LintIssue(
                    "warning",
                    "unknown-category",
                    f"question {question.id!r} uses undeclared category {question.category!r}",
                )
            )
        if question.gap is not None:
            for delta in question.gap.deltas:
                if delta.population is not None and delta.population not in pop_names:
                    issues.append(
                        LintIssue(
                            "error",
                            "gap-population",
                            f"question {question.id!r} gap targets unknown "
                            f"population {delta.population!r}",
                        )
                    )
                if delta.table is not None and delta.table not in table_names:
                    issues.append(
                        LintIssue(
                            "error",
                            "gap-table",
                            f"question {question.id!r} gap targets unknown table {delta.table!r}",
                        )
                    )


def projected_rows(
    spec: ScenarioSpec,
    *,
    scale: float = 1.0,
    sizes: Mapping[str, int] | None = None,
) -> int:
    """Total physical rows the spec would sample at ``scale``.

    Sizing matches the sampler exactly (``max(1, round(size * scale))``).
    ``sizes`` overrides per-population counts with *actual* sampled sizes —
    identity-bound populations' sizes are only known once the world is
    resolved, so the pre-sample estimate uses their declared base size.
    """
    total = 0
    for tbl in spec.tables:
        pop = spec.population(tbl.population)
        if sizes is not None and pop.name in sizes:
            size = sizes[pop.name]
        else:
            size = max(1, round(pop.size * scale))
        total += size if tbl.grain is TableGrain.ENTITY else size * spec.n_days
    return total


def lint_spec(spec: ScenarioSpec, *, scale: float = 1.0) -> list[LintIssue]:
    """Run every static check over ``spec``; return all findings."""
    issues: list[LintIssue] = []
    factor_names = [factor.name for factor in spec.factors]
    if len(set(factor_names)) != len(factor_names):
        issues.append(LintIssue("error", "duplicate-factor", "duplicate factor names"))
    pop_names = [pop.name for pop in spec.populations]
    if len(set(pop_names)) != len(pop_names):
        issues.append(LintIssue("error", "duplicate-population", "duplicate population names"))
    for pop in spec.populations:
        _lint_population(pop, spec, issues)
    _lint_tables(spec, issues)
    _lint_views_and_questions(spec, issues)

    try:
        rows = projected_rows(spec, scale=scale)
    except KeyError:
        rows = 0  # unknown population already reported
    if rows > MAX_PROJECTED_ROWS:
        issues.append(
            LintIssue(
                "error",
                "volume",
                f"projected {rows:,} rows exceeds the {MAX_PROJECTED_ROWS:,} guardrail",
            )
        )
    return issues


def require_clean(spec: ScenarioSpec, *, scale: float = 1.0) -> list[LintIssue]:
    """Lint ``spec`` and raise :class:`SpecLintError` on any error-severity issue.

    Returns the full issue list (warnings included) when clean.
    """
    issues = lint_spec(spec, scale=scale)
    if any(issue.severity == "error" for issue in issues):
        raise SpecLintError(issues)
    return issues
