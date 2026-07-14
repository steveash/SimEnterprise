"""Committed score baselines + compare semantics for ``reconstruct baseline`` (spec 0003 §2).

Regression tracking for the eval loop used to be hand-recorded markdown — the
BEFORE/AFTER tables in ``docs/RECONSTRUCT.md`` with a "paste the numbers into the
``_TBD_`` cells" instruction. Nothing failed when a change silently moved fidelity,
even though the fake-backend fidelity numbers are fully deterministic (D10/D31) and
carry real recall. This module replaces those snapshots with committed *baseline
cells* under ``evals/baselines/`` and a check/update harness that mirrors the
golden-pin convention (``tests/test_golden_run.py``) and the coverage ``fail_under``
floor: a deliberate metric move runs ``baseline update --reason "…"`` in the same
commit; ``check`` failing on main means an unreviewed behavior change.

A **baseline cell** is one ``(config, backend, seed)`` point with pinned metrics:

- **fake cells** (``mode: "exact"``): metrics are pure functions of a byte-repro
  golden/matrix run and a pure scorer, so the honest comparison is equality at 6 dp
  (guards last-ulp summation-reorder noise without hiding real movement). ``check``
  **regenerates** the cell keylessly (via :func:`~enterprise_sim.reconstruct.e2e.run_e2e`'s
  smoke path) and compares; any exceedance exits non-zero.
- **keyed cells** (``mode: "warn"``): answer-F1 from live models is nondeterministic,
  so comparison is ``abs(current − baseline) > tolerance ⇒ warn`` and ``check`` reads
  an existing e2e output dir's ``summary.json`` (``--against``) rather than
  regenerating. Warn cells never block PR CI; ``--strict`` turns a warn into a failure.

The committed cell this slice lands is ``golden-fake`` — the golden-run fidelity
headline over the fake backend. Its metrics are exactly the ``"fidelity"`` block of
the ``reconstruct e2e --keyless-smoke`` ``summary.json`` (§1), so a fake cell is a
projection of one e2e summary, not a second source of truth.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: The baseline file-format version (bumped only on a breaking schema change).
SCHEMA_VERSION = 1

#: The 11 fidelity headline metrics a fake cell pins — the exact key set the e2e
#: ``summary.json`` ``"fidelity"`` block carries
#: (:func:`enterprise_sim.reconstruct.e2e._build_summary`). Rates are floats; the
#: merge/count metrics are ints, and the JSON preserves that distinction.
FIDELITY_METRIC_KEYS: tuple[str, ...] = (
    "node_f1",
    "node_precision",
    "node_recall",
    "edge_f1",
    "edge_precision",
    "edge_recall",
    "provenance_f1",
    "over_merges",
    "under_merges",
    "reconstructed_nodes",
    "reconstructed_edges",
)

#: The per-system answer-F1 keys a keyed cell additionally pins, flattened from the
#: e2e ``summary.json`` ``"answer_f1"`` block (spec 0003 §2; F1). Keyed cells measure
#: the graph agent's *reasoning* advantage, not just reconstruction fidelity.
ANSWER_F1_METRIC_KEYS: tuple[str, ...] = (
    "answer_f1.oracle",
    "answer_f1.reconstructed",
    "answer_f1.rag",
)

#: The understanding/reasoning/total gap keys a keyed cell pins, flattened from the
#: ``"gaps"`` block — the attribution headline the keyed eval exists to track.
GAP_METRIC_KEYS: tuple[str, ...] = (
    "gaps.understanding",
    "gaps.reasoning",
    "gaps.total",
)

#: The full metric-key set a keyed cell pins: fidelity + answer-F1 + gaps (F1).
KEYED_METRIC_KEYS: tuple[str, ...] = FIDELITY_METRIC_KEYS + ANSWER_F1_METRIC_KEYS + GAP_METRIC_KEYS

#: The committed baselines live at the repo root (this file is
#: ``enterprise_sim/reconstruct/…``), tracked eval state referenced by both PR CI
#: and the keyed workflow — not tests. See the CLAUDE.md repo-map row for ``evals/``.
BASELINES_DIR: Path = Path(__file__).resolve().parents[2] / "evals" / "baselines"

#: Decimal places metrics are stored/compared at in ``exact`` mode (spec 0003 §2).
_EXACT_DP = 6


class BaselineCell(BaseModel):
    """One pinned ``(config, backend, seed)`` cell of the eval, serialized as JSON.

    The on-disk key is ``schema`` (the format version); it is exposed here as
    :attr:`schema_version` to avoid shadowing :class:`pydantic.BaseModel`'s own
    ``schema`` attribute, with the alias keeping the JSON key stable.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    cell: str
    backend: str
    source: str
    config: str
    seed: int
    mode: str
    tolerance: float
    metrics: dict[str, float | int]
    reason: str

    def to_json(self) -> str:
        """Serialize to sorted-keys, alias-keyed JSON (deterministic, one trailing newline)."""
        return json.dumps(self.model_dump(by_alias=True), indent=2, sort_keys=True) + "\n"

    @classmethod
    def read(cls, path: str | Path) -> BaselineCell:
        """Load a cell from ``path`` (accepts the ``schema`` alias key)."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class CellSpec:
    """The code-defined identity of a baseline cell — everything but its metrics.

    ``update`` writes these fields verbatim on every regeneration, so a cell's
    identity never drifts in the file: only ``metrics`` (regenerated) and ``reason``
    (the ``--reason`` text) are mutable state, mirroring how the golden pin and the
    ``fail_under`` floor keep their identity in code and only the value in the file.

    Attributes:
        cell: The cell name (its ``evals/baselines/<cell>.json`` basename).
        backend: The LLM backend the cell's metrics were produced under.
        config: The config the cell reconstructs (``examples/golden.toml`` today).
        seed: The run seed (a pure input of the byte-repro run).
        mode: ``"exact"`` (fake, equality at 6 dp) or ``"warn"`` (keyed, tolerance band).
        tolerance: The per-cell tolerance (0.0 for exact, F1 points for warn).
        keyed: ``True`` when metrics can only come from a keyed run (``--against``);
            ``False`` for a keylessly regenerable fake cell.
        metrics_shape: Which metric-key set the cell pins (F1) —
            ``"fidelity"`` (the 11 fidelity keys), ``"fidelity+answers"`` (fidelity +
            per-system answer-F1 + gaps, for keyed cells), or ``"matrix"`` (dynamic
            per-cell labels, validated against a regeneration rather than a static list).
            :func:`expected_metric_keys` turns it into the concrete key set the file's
            ``metrics`` must carry; ``metrics_from_summary`` extracts accordingly.
    """

    cell: str
    backend: str
    config: str
    seed: int
    mode: str
    tolerance: float
    keyed: bool
    metrics_shape: str = "fidelity"


#: Keylessly regenerable fake cells (``check``/``update`` rebuild + compare offline).
FAKE_CELLS: dict[str, CellSpec] = {
    "golden-fake": CellSpec(
        cell="golden-fake",
        backend="fake",
        config="examples/golden.toml",
        seed=7,
        mode="exact",
        tolerance=0.0,
        keyed=False,
    ),
    # The standing keyless matrix (spec 0003 §3): the first three catalog specs ×
    # seeds {7, 107} = 6 cells. Its metrics are the flattened per-cell + aggregate
    # fidelity of ``reconstruct scale --runs 3 --seeds 7,107`` (fake backend); the
    # ``config``/``seed`` fields are descriptive identity (the seed axis lives in the
    # config string, so ``seed`` records only the base axis value).
    "matrix-fake": CellSpec(
        cell="matrix-fake",
        backend="fake",
        config="reconstruct scale --runs 3 --seeds 7,107",
        seed=7,
        mode="exact",
        tolerance=0.0,
        keyed=False,
        metrics_shape="matrix",
    ),
}

#: Keyed cells — seeded from a live e2e run's ``summary.json`` (``--against``); not
#: committed until the first keyed workflow run supplies numbers (spec 0003 §2).
KEYED_CELLS: dict[str, CellSpec] = {
    "golden-keyed": CellSpec(
        cell="golden-keyed",
        backend="anthropic_api",
        config="examples/golden.toml",
        seed=7,
        mode="warn",
        tolerance=0.05,
        keyed=True,
        metrics_shape="fidelity+answers",
    ),
}

#: Every known cell, by name.
CELL_SPECS: dict[str, CellSpec] = {**FAKE_CELLS, **KEYED_CELLS}


def cell_path(cell: str) -> Path:
    """The committed file path for cell ``cell`` (``evals/baselines/<cell>.json``)."""
    return BASELINES_DIR / f"{cell}.json"


def _source_for(cell: str) -> str:
    """The provenance line stamped into a cell's ``source`` field (stable across updates)."""
    return f"enterprise-sim reconstruct baseline update --cell {cell}"


def expected_metric_keys(spec: CellSpec) -> tuple[str, ...] | None:
    """The concrete metric-key set a cell of ``spec``'s shape must pin (F1/F2).

    ``"fidelity"`` → the 11 fidelity keys; ``"fidelity+answers"`` → those plus the
    per-system answer-F1 and gap keys (keyed cells); ``"matrix"`` → ``None`` because
    the matrix's keys are dynamic per-cell labels, so its authoritative key set comes
    from a regeneration rather than a static list. Raises :class:`ValueError` for an
    unknown shape.
    """
    if spec.metrics_shape == "fidelity":
        return FIDELITY_METRIC_KEYS
    if spec.metrics_shape == "fidelity+answers":
        return KEYED_METRIC_KEYS
    if spec.metrics_shape == "matrix":
        return None
    raise ValueError(f"unknown metrics_shape {spec.metrics_shape!r} for cell {spec.cell!r}")


def regenerate_fake_metrics(spec: CellSpec) -> dict[str, float | int]:
    """Regenerate a fake cell's metrics keylessly, in a temp dir (no stored state).

    Runs the same ~0.8s keyless work the e2e smoke path does — a fresh golden run,
    the fake-backend reconstruction, and the pure fidelity scorer — and returns the
    ``summary.json`` ``"fidelity"`` block so the cell stays a projection of one e2e
    summary (identical rounding, one source of the metric projection). Raises
    :class:`ValueError` for a keyed cell (its numbers cannot be produced keylessly)
    or an unknown fake cell.
    """
    import tempfile

    if spec.keyed:
        raise ValueError(
            f"cell {spec.cell!r} is keyed (mode={spec.mode!r}); regenerate from a keyed "
            "run's summary.json via --against, not keylessly"
        )

    if spec.cell == "golden-fake":
        from enterprise_sim.reconstruct.e2e import run_e2e

        with tempfile.TemporaryDirectory(prefix="esim-baseline-") as tmp:
            result = run_e2e(Path(tmp) / "e2e", keyless_smoke=True)
        # Extract through the one projection path so a fake cell stays a projection of
        # one e2e summary (spec's metrics_shape decides which keys, F1).
        return metrics_from_summary(spec, result.summary)

    if spec.cell == "matrix-fake":
        # The standing matrix cell: reconstruct + score the 6-cell seeds matrix over
        # the fake backend and flatten it to the per-cell + aggregate metrics dict.
        from enterprise_sim.reconstruct.scale import (
            matrix_metrics,
            matrix_run_specs,
            run_scale,
        )

        with tempfile.TemporaryDirectory(prefix="esim-baseline-") as tmp:
            aggregate = run_scale(matrix_run_specs(), Path(tmp))
        return matrix_metrics(aggregate)

    raise ValueError(f"no keyless regenerator wired for fake cell {spec.cell!r}")


def metrics_from_summary(spec: CellSpec, summary: Mapping[str, object]) -> dict[str, float | int]:
    """Pull the metric keys ``spec`` pins out of an e2e ``summary.json`` payload (F1).

    A ``"fidelity"``-shaped cell (fake, and the fidelity half of any cell) takes just
    the fidelity block; a ``"fidelity+answers"`` cell (keyed) additionally flattens the
    ``"answer_f1"`` and ``"gaps"`` blocks to ``answer_f1.<slot>`` / ``gaps.<slot>`` keys
    — so a keyed baseline pins the reasoning advantage the eval exists to track, not
    just reconstruction fidelity. Raises :class:`KeyError` if a required block/key is
    absent; :class:`ValueError` for a ``"matrix"`` shape (its metrics are not summary-
    sourced — regenerate them via :func:`regenerate_fake_metrics`).
    """
    if spec.metrics_shape == "matrix":
        raise ValueError(
            f"cell {spec.cell!r} has a matrix shape; its metrics come from a scale "
            "regeneration, not a single summary.json"
        )
    fidelity = summary["fidelity"]
    if not isinstance(fidelity, Mapping):
        raise KeyError("summary.json has no 'fidelity' object")
    metrics: dict[str, float | int] = {key: fidelity[key] for key in FIDELITY_METRIC_KEYS}
    if spec.metrics_shape == "fidelity+answers":
        answer_f1 = summary["answer_f1"]
        gaps = summary["gaps"]
        if not isinstance(answer_f1, Mapping) or not isinstance(gaps, Mapping):
            raise KeyError("summary.json has no 'answer_f1'/'gaps' object")
        for slot in ("oracle", "reconstructed", "rag"):
            metrics[f"answer_f1.{slot}"] = answer_f1[slot]
        for slot in ("understanding", "reasoning", "total"):
            metrics[f"gaps.{slot}"] = gaps[slot]
    return metrics


def keyed_summary_problem(spec: CellSpec, summary: Mapping[str, object]) -> str | None:
    """Reject a ``--against`` summary that can't seed/check a keyed cell (F4).

    A keyed baseline must come from a *real keyed eval*: refuse a ``keyless-smoke``
    summary (its numbers are wiring stand-ins, not an eval) and refuse a summary whose
    ``backend`` disagrees with the cell's registered backend (a 1P baseline compared
    against a Bedrock run, or vice versa, is not a like-for-like drift). Returns a
    human-readable reason string, or ``None`` when the summary is a valid source.
    """
    mode = summary.get("mode")
    if mode == "keyless-smoke":
        return (
            f"summary.json mode is {mode!r} (keyless-smoke stand-in numbers, NOT an "
            "eval); a keyed baseline must come from a real keyed eval run"
        )
    backend = summary.get("backend")
    if backend != spec.backend:
        return (
            f"summary.json backend {backend!r} != cell {spec.cell!r} backend "
            f"{spec.backend!r}; a keyed baseline must compare like-for-like backends"
        )
    return None


def identity_mismatches(
    cell: BaselineCell, spec: CellSpec, current_keys: Iterable[str]
) -> list[str]:
    """Self-enforce a committed cell against its code-defined :class:`CellSpec` (F2).

    A cell file's ``mode``/``tolerance``/``backend`` and its metric-key set are
    *declarative documentation*; the :data:`CELL_SPECS` registry (and, for fake cells,
    the live regeneration's key set) is authoritative. Any divergence is a laundering
    vector — a hand-edited tolerance bump, a mode flip, a silently deleted metric — so
    this returns one message per mismatched field (empty ⇒ the file agrees with code).
    ``current_keys`` is the authoritative metric-key set: the regenerated keys for a
    fake cell, or the ``--against`` summary's extracted keys for a keyed cell.
    """
    problems: list[str] = []
    if cell.mode != spec.mode:
        problems.append(f"mode (file={cell.mode!r}, registry={spec.mode!r})")
    if cell.tolerance != spec.tolerance:
        problems.append(f"tolerance (file={cell.tolerance!r}, registry={spec.tolerance!r})")
    if cell.backend != spec.backend:
        problems.append(f"backend (file={cell.backend!r}, registry={spec.backend!r})")
    file_keys = set(cell.metrics)
    want_keys = set(current_keys)
    missing = sorted(want_keys - file_keys)
    if missing:
        problems.append(f"metrics missing from file (no silent shrinkage): {missing}")
    extra = sorted(file_keys - want_keys)
    if extra:
        problems.append(f"metrics in file but not produced by code: {extra}")
    return problems


def build_cell(spec: CellSpec, metrics: dict[str, float | int], reason: str) -> BaselineCell:
    """Assemble a :class:`BaselineCell` from its code-defined identity + fresh metrics."""
    return BaselineCell(
        cell=spec.cell,
        backend=spec.backend,
        source=_source_for(spec.cell),
        config=spec.config,
        seed=spec.seed,
        mode=spec.mode,
        tolerance=spec.tolerance,
        metrics=dict(metrics),
        reason=reason,
    )


@dataclass(frozen=True)
class MetricDrift:
    """One metric's baseline-vs-current comparison outcome.

    Attributes:
        metric: The metric key.
        expected: The pinned baseline value.
        actual: The regenerated / observed value.
        delta: ``actual − expected`` (rounded to 6 dp in ``exact`` mode).
        exceeded: ``True`` when ``abs(delta)`` breaches the cell's tolerance.
    """

    metric: str
    expected: float
    actual: float
    delta: float
    exceeded: bool


@dataclass(frozen=True)
class CompareResult:
    """The result of comparing a cell's pinned metrics against current values."""

    cell: str
    mode: str
    tolerance: float
    drifts: tuple[MetricDrift, ...]

    @property
    def exceedances(self) -> tuple[MetricDrift, ...]:
        """The drifts that breached tolerance (empty ⇒ the cell matches)."""
        return tuple(d for d in self.drifts if d.exceeded)

    @property
    def ok(self) -> bool:
        """``True`` when no metric breached tolerance."""
        return not self.exceedances


def compare(cell: BaselineCell, current: Mapping[str, float | int]) -> CompareResult:
    """Compare a cell's pinned ``metrics`` against ``current``, per the cell's mode.

    ``exact``: both values are rounded to 6 dp and any nonzero difference above
    ``tolerance`` (0.0) is an exceedance — the honest comparison for a byte-repro run
    through a pure scorer. ``warn``: the raw absolute difference above ``tolerance``
    is an exceedance (a live-model drift the caller may downgrade to a warning). A
    metric absent from ``current`` is reported as an exceedance rather than skipped.
    """
    exact = cell.mode == "exact"
    drifts: list[MetricDrift] = []
    for metric, expected_raw in cell.metrics.items():
        if metric not in current:
            drifts.append(
                MetricDrift(
                    metric=metric,
                    expected=float(expected_raw),
                    actual=float("nan"),
                    delta=float("nan"),
                    exceeded=True,
                )
            )
            continue
        actual_raw = current[metric]
        if exact:
            expected = round(float(expected_raw), _EXACT_DP)
            actual = round(float(actual_raw), _EXACT_DP)
        else:
            expected = float(expected_raw)
            actual = float(actual_raw)
        delta = actual - expected
        drifts.append(
            MetricDrift(
                metric=metric,
                expected=expected,
                actual=actual,
                delta=delta,
                exceeded=abs(delta) > cell.tolerance,
            )
        )
    return CompareResult(
        cell=cell.cell, mode=cell.mode, tolerance=cell.tolerance, drifts=tuple(drifts)
    )
