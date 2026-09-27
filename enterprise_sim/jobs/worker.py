"""The job worker: run/resume a world's build end to end (``docs/EXPLORER_RUNS.md`` §3.4).

``run_job`` is idempotent: it reads the world's ``.world``, opens a new
*segment* in its job ledger, builds an
:class:`~enterprise_sim.core.llm.client.LLMClient` from the recorded config
(``live`` from the ``.world``) against the world-local response cache, runs
:func:`~enterprise_sim.assembly.runner.execute_run` *into the same directory*
with the job's progress/control hooks, then finalizes on success by generating
the world's eval question set. A pause
(:class:`~enterprise_sim.jobs.control.RunPaused`) or any other exception never
propagates out of this function — both end the job in a terminal, resumable
status in the ``.world`` instead.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from enterprise_sim.assembly.runner import execute_run, llm_config_for
from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.llm import CostCeilingExceeded, LLMClient, build_client
from enterprise_sim.jobs.control import RunControl, RunPaused, install_signal_handlers
from enterprise_sim.jobs.progress import NullSink, ProgressEvent, ProgressSink
from enterprise_sim.jobs.state import JobState, Segment
from enterprise_sim.worlds.file import (
    WORLD_FILE,
    WorldFile,
    read_world,
    update_world,
    write_job_state,
)

__all__ = ["run_job", "write_run_evals"]

_EVALS_DIR = "evals"
_QUESTIONS_JSONL = "questions.jsonl"


def run_job(world_dir: Path, *, progress: ProgressSink | None = None) -> JobState:
    """Run or resume the build of the world at ``world_dir``; returns the final :class:`JobState`.

    Never raises for a build-level failure (a cost-ceiling breach or any other
    exception from the pipeline) — that becomes ``status="failed"`` with the
    error recorded in the ``.world``, still resumable. A cooperative pause ends
    the job ``status="paused"``, also resumable (a later ``run_job`` call re-runs
    the full pipeline; the world-local response cache serves whatever already
    rendered). Raises ``FileNotFoundError`` if ``world_dir`` holds no ``.world``.
    """
    world_dir = Path(world_dir)
    progress = progress or NullSink()
    world = read_world(world_dir)
    if world is None:
        raise FileNotFoundError(f"no {WORLD_FILE} in {world_dir}")
    config = WorldConfig.model_validate(world.config)

    control = RunControl(world_dir)
    control.clear()  # a fresh attempt starts un-paused, even after a prior pause.
    restore_signals = install_signal_handlers(control)

    state = world.job
    now = datetime.now(UTC).isoformat()
    if state.started_at is None:
        state.started_at = now
    state.status = "running"
    state.pid = os.getpid()
    state.error = None
    state.finished_at = None
    # A fresh segment's progress starts from zero even on a resume — the pipeline
    # re-renders every deliverable event (cache hits are free, §2), so "done" is
    # this attempt's count, not a carry-over from a prior one.
    state.phase = None
    state.artifacts_done = 0
    state.artifacts_cached = 0
    state.cost_usd_segment = 0.0
    write_job_state(world_dir, state)

    sink = _JobProgressSink(world_dir, state, progress)
    segment = Segment(started_at=now)
    client: LLMClient | None = None

    try:
        backend = config.model.backend.value if world.live else "fake"
        client = build_client(llm_config_for(config, backend=backend, world_dir=world_dir))
        result = execute_run(
            config, client=client, progress=sink, control=control, world_dir=world_dir
        )
    except RunPaused:
        assert client is not None  # a pause can only come from inside execute_run.
        _end_segment(state, segment, client, reason="paused")
        state.status = "paused"
        write_job_state(world_dir, state)
        progress.emit(
            ProgressEvent.now("paused", done=state.artifacts_done, total=state.artifacts_total)
        )
        return state
    except CostCeilingExceeded as exc:
        _fail(state, segment, client, progress, message=str(exc), error_type="CostCeilingExceeded")
        write_job_state(world_dir, state)
        return state
    except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised
        _fail(state, segment, client, progress, message=str(exc), error_type=type(exc).__name__)
        write_job_state(world_dir, state)
        return state
    finally:
        restore_signals()

    assert client is not None  # execute_run succeeded, so the client was built.
    _end_segment(state, segment, client, reason="done", artifacts=len(result.corpus.artifacts))
    state.phase = "done"
    state.artifacts_total = len(result.corpus.artifacts)
    state.artifacts_done = len(result.corpus.artifacts)

    if config.data.scenarios:
        progress.emit(ProgressEvent.now("phase", phase="data"))
        _build_data(world_dir, config, live=world.live, progress=progress)

    progress.emit(ProgressEvent.now("phase", phase="evals"))
    _finalize(world_dir, progress)

    state.status = "done"
    state.finished_at = datetime.now(UTC).isoformat()
    write_job_state(world_dir, state)
    progress.emit(
        ProgressEvent.now(
            "done",
            world_id=result.world_id,
            world_dir=str(world_dir),
            artifacts=len(result.corpus.artifacts),
            events=len(result.corpus.journal),
            cost_usd_segment=state.cost_usd_segment,
            cost_usd_total=state.cost_usd_total,
        )
    )
    return state


def _end_segment(
    state: JobState,
    segment: Segment,
    client: LLMClient,
    *,
    reason: str,
    artifacts: int = 0,
) -> None:
    """Close out ``segment`` with ``client``'s final totals and fold it into ``state``."""
    segment.ended_at = datetime.now(UTC).isoformat()
    segment.cost_usd = client.cost.total_cost_usd
    segment.artifacts_rendered = artifacts
    segment.reason = reason
    state.segments.append(segment)
    state.cost_usd_segment = client.cost.total_cost_usd
    state.cost_usd_total = sum(s.cost_usd for s in state.segments)
    state.usage_total = client.cost.total_usage.to_dict()


def _fail(
    state: JobState,
    segment: Segment,
    client: LLMClient | None,
    progress: ProgressSink,
    *,
    message: str,
    error_type: str,
) -> None:
    """Record a job-ending failure: close the segment (zeroed if no client was built yet)."""
    if client is not None:
        _end_segment(state, segment, client, reason="failed")
    else:
        segment.ended_at = datetime.now(UTC).isoformat()
        segment.reason = "failed"
        state.segments.append(segment)
        state.cost_usd_total = sum(s.cost_usd for s in state.segments)
    state.status = "failed"
    state.error = {"message": message, "type": error_type}
    state.finished_at = datetime.now(UTC).isoformat()
    progress.emit(ProgressEvent.now("error", message=message, type=error_type))


def _build_data(
    world_dir: Path, config: WorldConfig, *, live: bool, progress: ProgressSink
) -> None:
    """Build the world's structured data products (``config.data``) into ``<world>/data/``.

    Same window, seed, and KG as the corpus just built, so the tables and the
    documents describe one company. ``llm`` authoring uses the world's model on a
    live build and falls back to the deterministic template otherwise (like every
    keyless path). Never fails the build: a missing data extra or a scenario
    error is reported as a ``warning`` event and the corpus + KG stand.
    """
    plan = config.data
    try:
        from enterprise_sim.data_products import execute_data_run
        from enterprise_sim.data_products.config import DataRunConfig
    except ModuleNotFoundError as exc:
        progress.emit(
            ProgressEvent.now(
                "warning",
                message=(
                    f"structured data skipped: missing dependency {exc.name!r} — "
                    "install the data extra (uv sync --extra data)"
                ),
                type="ModuleNotFoundError",
            )
        )
        return

    backend = config.model.backend.value if live else "fake"
    data_config = DataRunConfig.model_validate(
        {
            "scenarios": list(plan.scenarios),
            "seed": config.seed,
            "world": {"world_dir": str(world_dir)},
            "window": {
                "start": config.simulation.period_start.isoformat(),
                "end": config.simulation.period_end.isoformat(),
            },
            "authoring": {"mode": plan.authoring.value},
            "scale": {
                "factor": plan.scale_factor,
                "rows_per_partition": plan.rows_per_partition,
            },
            "loop": {"iterations": plan.iterations},
            "model": {
                "backend": backend,
                "name": config.model.name,
                "cost_ceiling_usd": config.scale.cost_ceiling_usd,
            },
        }
    )

    def started(name: str, index: int, total: int) -> None:
        progress.emit(ProgressEvent.now("data_scenario", scenario=name, index=index, total=total))

    try:
        result = execute_data_run(data_config, world_dir=world_dir, on_scenario=started)
    except Exception as exc:  # noqa: BLE001 - data never fails the corpus build
        progress.emit(
            ProgressEvent.now(
                "warning",
                message=f"structured data failed: {type(exc).__name__}: {exc}",
                type=type(exc).__name__,
            )
        )
        return
    for scenario in result.scenarios:
        progress.emit(
            ProgressEvent.now(
                "data_done",
                scenario=scenario.scenario,
                rows=sum(scenario.rows_by_table.values()),
                tables=len(scenario.rows_by_table),
                questions_total=scenario.questions_total,
                questions_answerable=scenario.questions_answerable,
            )
        )


def _finalize(world_dir: Path, progress: ProgressSink) -> None:
    """Generate the world's eval questions and note the count in the ``.world``.

    Never allowed to fail the job: eval generation reports a ``warning`` event
    and moves on if it errors (§3.4).
    """
    count = write_run_evals(world_dir, progress=progress)
    if count is None:
        return

    def note(world: WorldFile) -> None:
        world.evals = {**world.evals, "questions": count}

    update_world(world_dir, note)


def write_run_evals(world_dir: Path, *, progress: ProgressSink | None = None) -> int | None:
    """Generate and write ``<world>/evals/questions.jsonl``; returns the question count.

    A small, swappable helper (kept separate from :func:`run_job` so the eval
    generator can change independently): calls
    :func:`enterprise_sim.benchmark.generate.generate` over the finished world
    and writes the resulting :class:`~enterprise_sim.benchmark.schema.Benchmark`
    via :meth:`~enterprise_sim.benchmark.schema.Benchmark.write_jsonl`. Any
    failure — the benchmark generator is deterministic but not infallible
    against every world shape — is reported as a ``warning`` progress event and
    yields ``None``, never raised, per ``docs/EXPLORER_RUNS.md`` §3.4.
    """
    progress = progress or NullSink()
    world_dir = Path(world_dir)
    try:
        from enterprise_sim.benchmark.generate import generate

        benchmark = generate(world_dir=world_dir)
        evals_dir = world_dir / _EVALS_DIR
        evals_dir.mkdir(parents=True, exist_ok=True)
        benchmark.write_jsonl(evals_dir / _QUESTIONS_JSONL)
        return len(benchmark.pairs)
    except Exception as exc:  # noqa: BLE001 - eval generation must never fail the job
        progress.emit(
            ProgressEvent.now(
                "warning",
                message=f"eval generation failed: {exc}",
                type=type(exc).__name__,
            )
        )
        return None


class _JobProgressSink:
    """Wraps a job's outer progress sink to keep the ``.world`` ledger live (§3.3).

    Applies every event to the shared :class:`JobState` and writes it atomically
    *before* forwarding the event to the caller-supplied sink — so a consumer
    reading the ``.world`` never observes it lagging behind a progress event it
    already saw on the stream. Thread-safe: the render phase emits ``artifact``
    events from several scenario-render threads at once.
    """

    def __init__(self, world_dir: Path, state: JobState, inner: ProgressSink) -> None:
        self._world_dir = world_dir
        self._state = state
        self._inner = inner
        self._lock = threading.Lock()

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self._apply(event)
            write_job_state(self._world_dir, self._state)
            if event.kind == "artifact":
                # The pipeline only knows this segment's spend; add the closed
                # segments' so a consumer never has to sum across files (§3.4).
                prior = sum(s.cost_usd for s in self._state.segments)
                event = ProgressEvent(
                    kind=event.kind,
                    ts=event.ts,
                    data={**event.data, "cost_usd_total": prior + self._state.cost_usd_segment},
                )
        self._inner.emit(event)

    def _apply(self, event: ProgressEvent) -> None:
        data = event.data
        state = self._state
        if event.kind == "phase":
            phase = data.get("phase")
            if isinstance(phase, str):
                state.phase = phase
        elif event.kind == "scheduled":
            total = data.get("artifacts_total")
            if isinstance(total, int):
                state.artifacts_total = total
        elif event.kind == "estimate":
            state.estimate = dict(data)
        elif event.kind == "artifact":
            done = data.get("done")
            total = data.get("total")
            if isinstance(done, int):
                state.artifacts_done = done
            if isinstance(total, int):
                state.artifacts_total = total
            if data.get("cached"):
                state.artifacts_cached += 1
            cost = data.get("cost_usd_segment")
            if isinstance(cost, int | float):
                state.cost_usd_segment = float(cost)
            usage = data.get("usage")
            if isinstance(usage, dict):
                state.usage_total = dict(usage)
