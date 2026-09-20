"""The job worker: run/resume a job end to end (``docs/EXPLORER_RUNS.md`` §3.4).

``run_job`` is idempotent: it loads ``config.json``, opens a new *segment* in
``state.json``, builds an :class:`~enterprise_sim.core.llm.client.LLMClient`
from the config (``live`` from ``job.json``), runs
:func:`~enterprise_sim.assembly.runner.execute_run` with the job's
progress/control hooks, then finalizes on success: writes ``lineage.json`` for
an extend job and generates the run's eval question set. A pause
(:class:`~enterprise_sim.jobs.control.RunPaused`) or any other exception never
propagates out of this function — both end the job in a terminal, resumable
``state.json`` status instead.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enterprise_sim.assembly.runner import RunResult, execute_run, llm_config_for
from enterprise_sim.core.config import RunConfig
from enterprise_sim.core.llm import CostCeilingExceeded, LLMClient, build_client
from enterprise_sim.jobs.control import RunControl, RunPaused, install_signal_handlers
from enterprise_sim.jobs.extend import copy_cache_on_start, resolve_parent_cache_dir
from enterprise_sim.jobs.progress import NullSink, ProgressEvent, ProgressSink
from enterprise_sim.jobs.state import JobState, Segment, read_state, write_state

__all__ = ["run_job", "write_run_evals"]

_JOB_JSON = "job.json"
_CONFIG_JSON = "config.json"
_LINEAGE_JSON = "lineage.json"
_EVALS_DIR = "evals"
_QUESTIONS_JSONL = "questions.jsonl"


def run_job(job_dir: Path, *, progress: ProgressSink | None = None) -> JobState:
    """Run or resume the job at ``job_dir``; returns the final :class:`JobState`.

    Never raises for a run-level failure (a cost-ceiling breach or any other
    exception from the pipeline) — that becomes ``status="failed"`` with the
    error recorded in ``state.json``, still resumable. A cooperative pause ends
    the job ``status="paused"``, also resumable (a later ``run_job`` call re-runs
    the full pipeline; the response cache serves whatever already rendered).
    """
    job_dir = Path(job_dir)
    progress = progress or NullSink()
    job = json.loads((job_dir / _JOB_JSON).read_text(encoding="utf-8"))
    config = RunConfig.model_validate(
        json.loads((job_dir / _CONFIG_JSON).read_text(encoding="utf-8"))
    )

    control = RunControl(job_dir)
    control.clear()  # a fresh attempt starts un-paused, even after a prior pause.
    restore_signals = install_signal_handlers(control)

    state = read_state(job_dir) or JobState()
    now = datetime.now(UTC).isoformat()
    if state.started_at is None:
        state.started_at = now
    state.status = "running"
    state.pid = os.getpid()
    state.error = None
    # A fresh segment's progress starts from zero even on a resume — the pipeline
    # re-renders every deliverable event (cache hits are free, §2), so "done" is
    # this attempt's count, not a carry-over from a prior one.
    state.phase = None
    state.artifacts_done = 0
    state.artifacts_cached = 0
    state.cost_usd_segment = 0.0
    write_state(job_dir, state)

    sink = _JobProgressSink(job_dir, state, progress)
    segment = Segment(started_at=now)
    client: LLMClient | None = None

    try:
        if job.get("kind") == "extend" and job.get("parent_run_dir") and config.scale.cache_dir:
            parent_cache = resolve_parent_cache_dir(
                Path(job["parent_run_dir"]), jobs_root=job_dir.parent
            )
            copy_cache_on_start(parent_cache, Path(config.scale.cache_dir))

        live = bool(job.get("live", False))
        backend = config.model.backend.value if live else "fake"
        client = build_client(llm_config_for(config, backend=backend))

        result = execute_run(config, client=client, progress=sink, control=control)
    except RunPaused:
        assert client is not None  # a pause can only come from inside execute_run.
        _end_segment(state, segment, client, reason="paused")
        state.status = "paused"
        write_state(job_dir, state)
        progress.emit(
            ProgressEvent.now("paused", done=state.artifacts_done, total=state.artifacts_total)
        )
        return state
    except CostCeilingExceeded as exc:
        _fail(state, segment, client, progress, message=str(exc), error_type="CostCeilingExceeded")
        write_state(job_dir, state)
        return state
    except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised
        _fail(state, segment, client, progress, message=str(exc), error_type=type(exc).__name__)
        write_state(job_dir, state)
        return state
    finally:
        restore_signals()

    assert client is not None  # execute_run succeeded, so the client was built.
    _end_segment(state, segment, client, reason="done", artifacts=len(result.corpus.artifacts))
    state.run_id = result.run_id
    state.run_dir = str(result.run_dir)
    state.phase = "done"
    state.artifacts_total = len(result.corpus.artifacts)
    state.artifacts_done = len(result.corpus.artifacts)

    progress.emit(ProgressEvent.now("phase", phase="evals"))
    _finalize(job_dir, job, result, progress)

    state.status = "done"
    state.finished_at = datetime.now(UTC).isoformat()
    write_state(job_dir, state)
    progress.emit(
        ProgressEvent.now(
            "done",
            run_id=result.run_id,
            run_dir=str(result.run_dir),
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


def _finalize(
    job_dir: Path,
    job: dict[str, Any],
    result: RunResult,
    progress: ProgressSink,
) -> None:
    """Write ``lineage.json`` (extend jobs) and generate the run's eval questions.

    Neither step is allowed to fail the job: a lineage write only happens for an
    extend job whose parent is known, and eval generation reports a ``warning``
    event and moves on if it errors (§3.4).
    """
    if job.get("kind") == "extend" and job.get("parent_run_dir"):
        parent_run_dir = str(job["parent_run_dir"])
        lineage = {
            "parent_run_id": Path(parent_run_dir).name,
            "parent_run_dir": parent_run_dir,
            "changes": job.get("changes") or {},
        }
        (result.run_dir / _LINEAGE_JSON).write_text(
            json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    write_run_evals(result.run_dir, progress=progress)


def write_run_evals(run_dir: Path, *, progress: ProgressSink | None = None) -> None:
    """Generate and write ``<run>/evals/questions.jsonl`` (never fails the caller).

    A small, swappable helper (kept separate from :func:`run_job` so the eval
    generator can change independently): calls
    :func:`enterprise_sim.benchmark.generate.generate` over the finished run and
    writes the resulting :class:`~enterprise_sim.benchmark.schema.Benchmark` via
    :meth:`~enterprise_sim.benchmark.schema.Benchmark.write_jsonl`. Any failure —
    the benchmark generator is deterministic but not infallible against every
    world shape — is reported as a ``warning`` progress event, never raised, per
    ``docs/EXPLORER_RUNS.md`` §3.4.
    """
    progress = progress or NullSink()
    run_dir = Path(run_dir)
    try:
        from enterprise_sim.benchmark.generate import generate

        benchmark = generate(run_dir=run_dir)
        evals_dir = run_dir / _EVALS_DIR
        evals_dir.mkdir(parents=True, exist_ok=True)
        benchmark.write_jsonl(evals_dir / _QUESTIONS_JSONL)
    except Exception as exc:  # noqa: BLE001 - eval generation must never fail the job
        progress.emit(
            ProgressEvent.now(
                "warning",
                message=f"eval generation failed: {exc}",
                type=type(exc).__name__,
            )
        )


class _JobProgressSink:
    """Wraps a job's outer progress sink to keep ``state.json`` live (§3.3).

    Applies every event to the shared :class:`JobState` and writes it atomically
    *before* forwarding the event to the caller-supplied sink — so a consumer
    reading ``state.json`` never observes it lagging behind a progress event it
    already saw on the stream. Thread-safe: the render phase emits ``artifact``
    events from several scenario-render threads at once.
    """

    def __init__(self, job_dir: Path, state: JobState, inner: ProgressSink) -> None:
        self._job_dir = job_dir
        self._state = state
        self._inner = inner
        self._lock = threading.Lock()

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self._apply(event)
            write_state(self._job_dir, self._state)
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
