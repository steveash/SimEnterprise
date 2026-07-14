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
from collections.abc import Mapping
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
    """

    cell: str
    backend: str
    config: str
    seed: int
    mode: str
    tolerance: float
    keyed: bool


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
        fidelity = result.summary["fidelity"]
        return {key: fidelity[key] for key in FIDELITY_METRIC_KEYS}

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


def metrics_from_summary(summary: Mapping[str, object]) -> dict[str, float | int]:
    """Pull the pinned metric keys out of an e2e ``summary.json`` payload.

    Used by the keyed ``--against`` path: reads the ``"fidelity"`` block of an
    existing e2e output dir's summary (keyed numbers cannot be regenerated
    keylessly). Raises :class:`KeyError` if the summary is missing a pinned key.
    """
    fidelity = summary["fidelity"]
    if not isinstance(fidelity, Mapping):
        raise KeyError("summary.json has no 'fidelity' object")
    return {key: fidelity[key] for key in FIDELITY_METRIC_KEYS}


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
