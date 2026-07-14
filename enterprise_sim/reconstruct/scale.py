"""Multi-run scale harness + aggregate fidelity report (esim-ecr.5).

Everything upstream of this module evaluates reconstruction on *one* tiny golden
run: one synthetic company, one archetype, 56 nodes. That is enough to prove the
pipeline works, but not enough to say how it *generalizes* — a single run can't
distinguish "the reconstructor is good" from "the reconstructor happens to fit
this one company". This module closes that gap. It generates several *varied*
runs (different archetype — engineering vs retail — and size band), reconstructs
and scores each with the existing pipeline, and **aggregates** the per-run
fidelity into a mean/spread report.

The design reuses the whole stack unchanged:

* :func:`~enterprise_sim.assembly.execute_run` builds each varied gold run from a
  seeded :class:`~enterprise_sim.core.config.RunConfig` (the same entry point
  ``tests/test_golden_run.py`` drives), so the gold graph is always deterministic
  and network-free regardless of the reconstruction backend;
* :func:`~enterprise_sim.reconstruct.build.run_pipeline` reconstructs each run's
  corpus (its LLM extraction/adjudication steps are the *gated* part — a ``fake``
  client reconstructs keylessly, a real client runs the keyed extraction);
* :func:`~enterprise_sim.reconstruct.fidelity.score_fidelity` scores each
  reconstruction against its own gold graph.

The run *generation* is deterministic (seeded configs from a fixed catalog), and
only the reconstruction client is a backend choice — so the harness runs end to
end with no key: it generates ≥2 varied runs and aggregates their fidelity via the
``fake`` backend in keyless CI, while a real keyed aggregation is just the same
call with an ``anthropic_api`` client (a crew run).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from enterprise_sim.assembly import execute_run
from enterprise_sim.core.config import (
    CompanyConfig,
    CompanySize,
    RunConfig,
    SimulationConfig,
)
from enterprise_sim.core.llm import LLMClient, LLMConfig, build_client
from enterprise_sim.reconstruct.build import (
    DEFAULT_BUILD_CONFIG,
    BuildConfig,
    run_pipeline,
)
from enterprise_sim.reconstruct.extract import HAIKU_MODEL
from enterprise_sim.reconstruct.fidelity import FidelityReport, score_fidelity

__all__ = [
    "MATRIX_RUNS",
    "MATRIX_SEEDS",
    "Aggregate",
    "AggregateFidelity",
    "RunFidelity",
    "RunSpec",
    "default_run_specs",
    "matrix_metrics",
    "matrix_run_specs",
    "reconstruct_and_score",
    "run_scale",
]


# A single business week (Mon 2026-01-05 .. Fri 2026-01-09) — the same window the
# golden run uses. Every generated spec shares it so runs differ by archetype/size,
# not by calendar length; the harness stays small and fast.
_WINDOW_START = date(2026, 1, 5)
_WINDOW_END = date(2026, 1, 9)


@dataclass(frozen=True)
class RunSpec:
    """A varied run to generate: an archetype (via ``vertical``), size, and seed.

    A spec is a deterministic recipe for one gold run. :meth:`to_config` projects
    it onto the same :class:`~enterprise_sim.core.config.RunConfig` a hand-written
    ``.toml`` would produce, so the harness drives
    :func:`~enterprise_sim.assembly.execute_run` exactly as a normal run does.

    Attributes:
        label: Stable, filesystem-safe identifier for this spec (e.g.
            ``"engineering-startup"``); names the run's work dir and its report row.
        company_name: Display name for the synthetic company.
        vertical: Company vertical string; selects the primary archetype
            (``"software"`` → engineering, ``"retail"`` → retail) per
            ``enterprise_sim.world_builders.builder``.
        size: Company-size band, biasing org depth and headcount.
        seed: Root seed threaded through the run — different seeds vary the world
            even at a fixed (vertical, size).
        period_start / period_end: The simulation window (inclusive).
    """

    label: str
    company_name: str
    vertical: str
    size: CompanySize
    seed: int
    period_start: date = _WINDOW_START
    period_end: date = _WINDOW_END

    def to_config(self, output_dir: str | Path) -> RunConfig:
        """Project the spec onto a :class:`RunConfig` landing in ``output_dir``."""
        return RunConfig(
            company=CompanyConfig(
                name=self.company_name,
                vertical=self.vertical,
                size=self.size,
                description=f"Scale-harness run: {self.label}.",
            ),
            simulation=SimulationConfig(
                period_start=self.period_start,
                period_end=self.period_end,
            ),
            seed=self.seed,
            output_dir=Path(output_dir),
        )


# The catalog the harness draws varied runs from, ordered so the first two already
# vary the archetype (engineering vs retail) — the primary axis the epic names.
# Later entries add size bands so a larger ``count`` keeps introducing variety
# rather than re-running one company.
_SPEC_CATALOG: tuple[tuple[str, str, str, CompanySize], ...] = (
    ("engineering-startup", "Reconstruct Eng Co", "software", CompanySize.STARTUP),
    ("retail-startup", "Reconstruct Retail Co", "retail", CompanySize.STARTUP),
    ("engineering-small", "Reconstruct Eng Group", "software", CompanySize.SMALL),
    ("retail-small", "Reconstruct Retail Group", "retail", CompanySize.SMALL),
    ("engineering-medium", "Reconstruct Eng Holdings", "software", CompanySize.MEDIUM),
    ("retail-medium", "Reconstruct Retail Holdings", "retail", CompanySize.MEDIUM),
)


def default_run_specs(count: int = 2, *, seed: int = 7) -> list[RunSpec]:
    """The first ``count`` varied specs from the built-in catalog (deterministic).

    Draws from a fixed catalog whose leading entries already vary the archetype
    (engineering, then retail), so even the minimum ``count == 2`` exercises two
    distinct playbooks. Each spec's seed is ``seed + index`` so runs are
    independent yet reproducible. ``count`` must be in ``1..len(catalog)``.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    if count > len(_SPEC_CATALOG):
        raise ValueError(f"count must be <= {len(_SPEC_CATALOG)} (the catalog size), got {count}")
    specs: list[RunSpec] = []
    for index, (label, company, vertical, size) in enumerate(_SPEC_CATALOG[:count]):
        specs.append(
            RunSpec(
                label=label,
                company_name=company,
                vertical=vertical,
                size=size,
                seed=seed + index,
            )
        )
    return specs


#: The standing keyless matrix's shape (spec 0003 §3): the first three catalog specs
#: — engineering-startup, retail-startup, engineering-small (two archetypes, two size
#: bands) — crossed with seeds {7, 107}, i.e. 6 cells. Small on purpose so the whole
#: matrix regenerates well inside the CI e2e-smoke bound; growing it is a
#: baseline-update, not a design change. ``matrix-fake.json`` pins this exact set.
MATRIX_RUNS = 3
MATRIX_SEEDS: tuple[int, ...] = (7, 107)


def matrix_run_specs(
    count: int = MATRIX_RUNS, seeds: Sequence[int] = MATRIX_SEEDS
) -> list[RunSpec]:
    """The seeds-axis matrix: the first ``count`` catalog specs × ``seeds`` (spec 0003 §3).

    Each ``(catalog entry, seed)`` pair is one matrix cell — a :class:`RunSpec`
    carrying the catalog entry's archetype/size with its ``seed`` set to the axis
    value and its ``label`` suffixed ``-s<seed>`` (so ``engineering-startup`` at seed
    107 is ``engineering-startup-s107``). Order is stable and deterministic: catalog
    entry outer, seed inner. ``count`` must be in ``1..len(catalog)`` (validated by
    :func:`default_run_specs`); ``seeds`` must be non-empty with no duplicates.
    """
    if not seeds:
        raise ValueError("matrix_run_specs needs at least one seed")
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"seeds must be unique, got {list(seeds)}")
    specs: list[RunSpec] = []
    for base in default_run_specs(count):
        for seed in seeds:
            specs.append(replace(base, label=f"{base.label}-s{seed}", seed=seed))
    return specs


@dataclass(frozen=True)
class RunFidelity:
    """One varied run's fidelity outcome: its spec label and scored report."""

    label: str
    vertical: str
    size: str
    run_id: str
    report: FidelityReport

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of this run's fidelity."""
        return {
            "label": self.label,
            "vertical": self.vertical,
            "size": self.size,
            "run_id": self.run_id,
            "fidelity": self.report.to_dict(),
        }


@dataclass(frozen=True)
class Aggregate:
    """Mean/spread of one metric across the runs.

    Attributes:
        mean: Arithmetic mean of the metric over all runs.
        stdev: Population standard deviation (0.0 for a single run).
        minimum / maximum: The metric's range across runs.
        n: Number of runs contributing.
    """

    mean: float
    stdev: float
    minimum: float
    maximum: float
    n: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "max": self.maximum,
            "n": self.n,
        }


def _aggregate(values: Sequence[float]) -> Aggregate:
    """Mean, population stdev, and range of ``values`` (``values`` is non-empty)."""
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return Aggregate(
        mean=mean,
        stdev=math.sqrt(variance),
        minimum=min(values),
        maximum=max(values),
        n=n,
    )


# The scalar metrics aggregated across runs, each a ``(key, extractor)`` pair. The
# order is the report's row order.
_METRICS: tuple[tuple[str, Any], ...] = (
    ("node_f1", lambda r: r.nodes.overall.f1),
    ("node_precision", lambda r: r.nodes.overall.precision),
    ("node_recall", lambda r: r.nodes.overall.recall),
    ("edge_f1", lambda r: r.edges.overall.f1),
    ("edge_precision", lambda r: r.edges.overall.precision),
    ("edge_recall", lambda r: r.edges.overall.recall),
    ("over_merges", lambda r: float(r.entity_resolution.over_merges)),
    ("under_merges", lambda r: float(r.entity_resolution.under_merges)),
    ("reconstructed_nodes", lambda r: float(r.reconstructed_node_count)),
    ("gold_nodes", lambda r: float(r.gold_node_count)),
    ("reconstructed_edges", lambda r: float(r.reconstructed_edge_count)),
    ("gold_edges", lambda r: float(r.gold_edge_count)),
)


@dataclass(frozen=True)
class AggregateFidelity:
    """Per-run fidelity plus the mean/spread aggregate across all runs."""

    runs: tuple[RunFidelity, ...]
    metrics: dict[str, Aggregate]
    backend: str

    @property
    def run_count(self) -> int:
        """Number of varied runs aggregated."""
        return len(self.runs)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the whole aggregate."""
        return {
            "run_count": self.run_count,
            "backend": self.backend,
            "runs": [run.to_dict() for run in self.runs],
            "aggregate": {key: agg.to_dict() for key, agg in self.metrics.items()},
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the aggregate to a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Render the aggregate as a GitHub-flavored markdown document."""
        return render_markdown(self)


def build_aggregate(runs: Sequence[RunFidelity], *, backend: str) -> AggregateFidelity:
    """Aggregate per-run fidelity into mean/spread over every scalar metric.

    ``runs`` must be non-empty. Each metric in :data:`_METRICS` is extracted from
    every run's :class:`FidelityReport` and reduced to an :class:`Aggregate` (mean,
    population stdev, min, max). Pure — the same runs always aggregate identically.
    """
    if not runs:
        raise ValueError("cannot aggregate zero runs")
    metrics = {
        key: _aggregate([extractor(run.report) for run in runs]) for key, extractor in _METRICS
    }
    return AggregateFidelity(runs=tuple(runs), metrics=metrics, backend=backend)


# The per-cell fidelity headline pinned in the standing matrix baseline: the same
# rate + count metrics a golden cell carries, minus provenance (the matrix runs
# score without a grounding key). ``matrix_metrics`` prefixes each with the cell's
# label so every cell is pinned independently — a regression in one cell fails even
# if the aggregate mean is unmoved.
_MATRIX_CELL_METRICS: tuple[tuple[str, Any], ...] = (
    ("node_f1", lambda r: r.nodes.overall.f1),
    ("node_precision", lambda r: r.nodes.overall.precision),
    ("node_recall", lambda r: r.nodes.overall.recall),
    ("edge_f1", lambda r: r.edges.overall.f1),
    ("edge_precision", lambda r: r.edges.overall.precision),
    ("edge_recall", lambda r: r.edges.overall.recall),
    ("over_merges", lambda r: r.entity_resolution.over_merges),
    ("under_merges", lambda r: r.entity_resolution.under_merges),
    ("reconstructed_nodes", lambda r: r.reconstructed_node_count),
    ("reconstructed_edges", lambda r: r.reconstructed_edge_count),
)

#: The aggregate rate means pinned across the matrix (the "aggregate" half of the
#: per-cell + aggregate baseline). Counts/merges aggregate trivially from the
#: per-cell pins, so only the headline rates are summarised here.
_MATRIX_AGGREGATE_KEYS: tuple[str, ...] = (
    "node_f1",
    "node_precision",
    "node_recall",
    "edge_f1",
    "edge_precision",
    "edge_recall",
)


def matrix_metrics(aggregate: AggregateFidelity) -> dict[str, float | int]:
    """Flatten a matrix aggregate into the flat ``metrics`` dict a baseline cell pins.

    Per cell (keyed ``<label>.<metric>``) the core rate + count fidelity headline,
    then the aggregate rate means (keyed ``aggregate.<metric>_mean``) — the
    "per-cell + aggregate" the standing matrix baseline commits (spec 0003 §3).
    Rates are rounded to 6 dp (the exact-mode convention, matching the golden cell's
    :func:`enterprise_sim.reconstruct.e2e._round`); counts/merges stay ints so the
    JSON preserves the distinction. Pure: the same aggregate flattens identically.
    """
    metrics: dict[str, float | int] = {}
    for run in aggregate.runs:
        for key, extractor in _MATRIX_CELL_METRICS:
            value = extractor(run.report)
            metrics[f"{run.label}.{key}"] = round(value, 6) if isinstance(value, float) else value
    for key in _MATRIX_AGGREGATE_KEYS:
        metrics[f"aggregate.{key}_mean"] = round(aggregate.metrics[key].mean, 6)
    return metrics


def reconstruct_and_score(
    spec: RunSpec,
    work_dir: str | Path,
    client: LLMClient,
    *,
    model: str | None = HAIKU_MODEL,
    build_config: BuildConfig = DEFAULT_BUILD_CONFIG,
) -> RunFidelity:
    """Generate one gold run, reconstruct its corpus, and score the fidelity.

    Executes ``spec`` into ``work_dir`` with the deterministic ``fake`` sim backend
    (so the gold graph is reproducible no matter the reconstruction client), runs
    the reconstruct pipeline over its raw corpus through ``client`` (the gated LLM
    step — ``fake`` reconstructs keylessly), and scores the result against the run's
    own gold world. Returns the labelled :class:`RunFidelity`.
    """
    result = execute_run(spec.to_config(work_dir))
    kg = run_pipeline(str(result.run_dir), client, model=model, config=build_config)
    report = score_fidelity(kg, result.world)
    return RunFidelity(
        label=spec.label,
        vertical=spec.vertical,
        size=spec.size.value,
        run_id=result.run_id,
        report=report,
    )


def run_scale(
    specs: Iterable[RunSpec],
    work_dir: str | Path,
    *,
    backend: str = "fake",
    model: str | None = HAIKU_MODEL,
    build_config: BuildConfig = DEFAULT_BUILD_CONFIG,
    client: LLMClient | None = None,
) -> AggregateFidelity:
    """Reconstruct + score every spec and aggregate the fidelity (the harness).

    Runs :func:`reconstruct_and_score` for each spec under its own subdirectory of
    ``work_dir`` (named by the spec label), then aggregates the per-run reports into
    an :class:`AggregateFidelity`. The reconstruction ``client`` is built from
    ``backend``/``model`` unless one is passed explicitly; the sim step always uses
    the deterministic ``fake`` backend inside :func:`reconstruct_and_score`. At
    least one spec is required.
    """
    spec_list = list(specs)
    if not spec_list:
        raise ValueError("run_scale needs at least one RunSpec")

    if client is not None:
        reconstruct_client = client
    else:
        # ``model=None`` means "the pipeline default"; let LLMConfig keep its own
        # default model rather than forcing None onto its str field.
        llm_config = (
            LLMConfig(backend=backend) if model is None else LLMConfig(backend=backend, model=model)
        )
        reconstruct_client = build_client(llm_config)
    root = Path(work_dir)
    runs: list[RunFidelity] = []
    for spec in spec_list:
        runs.append(
            reconstruct_and_score(
                spec,
                root / spec.label,
                reconstruct_client,
                model=model,
                build_config=build_config,
            )
        )
    return build_aggregate(runs, backend=backend)


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    """Render a metric to three decimals (matching the fidelity report)."""
    return f"{value:.3f}"


def _markdown_table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    """A GitHub-flavored markdown table (header + separator + body rows)."""
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_markdown(
    aggregate: AggregateFidelity, *, title: str = "Reconstruct scale — aggregate fidelity"
) -> str:
    """Render an :class:`AggregateFidelity` as a markdown document.

    A per-run table (label / vertical / size / node & edge F1 / sizes) and an
    aggregate table (mean, stdev, min, max per metric). Pure: the same aggregate
    always renders the same text.
    """
    lines = [
        f"# {title}",
        "",
        (
            f"{aggregate.run_count} varied runs reconstructed and scored "
            f"(reconstruction backend: {aggregate.backend})."
        ),
        "",
        "## Per run",
        "",
    ]
    per_run_header = [
        "run",
        "vertical",
        "size",
        "node F1",
        "edge F1",
        "recon/gold nodes",
        "recon/gold edges",
    ]
    per_run_rows = [
        [
            run.label,
            run.vertical,
            run.size,
            _fmt(run.report.nodes.overall.f1),
            _fmt(run.report.edges.overall.f1),
            f"{run.report.reconstructed_node_count}/{run.report.gold_node_count}",
            f"{run.report.reconstructed_edge_count}/{run.report.gold_edge_count}",
        ]
        for run in aggregate.runs
    ]
    lines.extend(_markdown_table(per_run_header, per_run_rows))

    lines.extend(["", "## Aggregate (across runs)", ""])
    agg_header = ["metric", "mean", "stdev", "min", "max"]
    agg_rows = [
        [
            key,
            _fmt(agg.mean),
            _fmt(agg.stdev),
            _fmt(agg.minimum),
            _fmt(agg.maximum),
        ]
        for key, agg in aggregate.metrics.items()
    ]
    lines.extend(_markdown_table(agg_header, agg_rows))

    return "\n".join(lines) + "\n"
