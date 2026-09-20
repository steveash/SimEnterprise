"""Cooperative pause/resume and the cost ledger across segments (``EXPLORER_RUNS.md`` §3.2/§6)."""

from __future__ import annotations

import json
from pathlib import Path

from enterprise_sim.assembly import execute_run
from enterprise_sim.assembly.manifest import structural_view
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.jobs.control import RunControl
from enterprise_sim.jobs.progress import NullSink, ProgressEvent
from enterprise_sim.jobs.state import JobState, write_state
from enterprise_sim.jobs.worker import run_job


def _config(output_dir: Path, *, seed: int = 7, cache_dir: Path | None = None) -> RunConfig:
    config = load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "output_dir": str(output_dir),
        }
    )
    scale = config.scale.model_copy(
        update={
            # Deterministic exact-N pause counts need one artifact rendered at a
            # time (§6: "Use max_concurrency = 1 in that test").
            "max_concurrency": 1,
            "cache_dir": str(cache_dir) if cache_dir is not None else None,
        }
    )
    return config.model_copy(update={"scale": scale})


def _create_job(jobs_root: Path, config: RunConfig, *, job_id: str = "job-1") -> Path:
    """Write a minimal job directory the way ``job create`` would (§3.3).

    In particular, ``scale.cache_dir`` is forced to ``<job>/llm-cache`` when the
    config did not set one, so the response cache actually persists across the
    segments of a paused-then-resumed job — the mechanism the reuse guarantee
    (§2) depends on.
    """
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    if config.scale.cache_dir is None:
        config = config.model_copy(
            update={
                "scale": config.scale.model_copy(update={"cache_dir": str(job_dir / "llm-cache")})
            }
        )
    now = "2026-01-01T00:00:00+00:00"
    job_payload = {
        "kind": "new",
        "live": False,
        "config": config.model_dump(mode="json"),
        "parent_run_dir": None,
        "changes": None,
        "created_at": now,
    }
    (job_dir / "job.json").write_text(json.dumps(job_payload, sort_keys=True), encoding="utf-8")
    (job_dir / "config.json").write_text(
        json.dumps(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    write_state(job_dir, JobState(status="created", started_at=now))
    return job_dir


class _PauseAfterN:
    """A progress sink that requests a pause once N ``artifact`` events were seen."""

    def __init__(self, job_dir: Path, n: int) -> None:
        self._job_dir = job_dir
        self._n = n
        self._count = 0
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)
        if event.kind == "artifact":
            self._count += 1
            if self._count == self._n:
                RunControl(self._job_dir).request_pause()


def _blob(run_dir: Path, subdirs: tuple[str, ...]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for sub in subdirs:
        for path in sorted((run_dir / sub).rglob("*")):
            if path.is_file():
                out[str(path.relative_to(run_dir))] = path.read_bytes()
    return out


def test_worker_run_job_completes_uninterrupted(tmp_path: Path) -> None:
    config = _config(tmp_path / "run")
    job_dir = _create_job(tmp_path / "jobs", config)

    state = run_job(job_dir, progress=NullSink())

    assert state.status == "done"
    assert state.run_dir is not None
    assert Path(state.run_dir).is_dir()
    assert state.artifacts_done == state.artifacts_total > 0
    assert len(state.segments) == 1
    assert state.segments[0].reason == "done"
    assert state.cost_usd_total == state.cost_usd_segment == state.segments[0].cost_usd


def test_pause_mid_render_then_resume_matches_uninterrupted_run(tmp_path: Path) -> None:
    # A control-connected run of the golden-sized config to learn the total
    # artifact count (so the pause point is a real mid-render point, not past
    # the end) and to have something to diff the resumed job against.
    baseline_config = _config(tmp_path / "baseline")
    baseline = execute_run(baseline_config)
    total_artifacts = len(baseline.corpus.artifacts)
    assert total_artifacts >= 3, "need multiple artifacts for a meaningful mid-render pause"
    pause_after = total_artifacts - 1

    config = _config(tmp_path / "job-run")
    job_dir = _create_job(tmp_path / "jobs", config)
    sink = _PauseAfterN(job_dir, pause_after)

    paused_state = run_job(job_dir, progress=sink)

    assert paused_state.status == "paused"
    # max_concurrency=1 (§6) makes this an exact count, not a range.
    assert paused_state.artifacts_done == pause_after
    assert paused_state.artifacts_total == total_artifacts
    assert len(paused_state.segments) == 1
    assert paused_state.segments[0].reason == "paused"
    paused_events = [e for e in sink.events if e.kind == "paused"]
    assert len(paused_events) == 1
    assert paused_events[0].data == {"done": pause_after, "total": total_artifacts}

    # Resume: run_job is idempotent — it re-runs the full pipeline; the on-disk
    # response cache (shared llm-cache under the job dir across both attempts)
    # serves whatever already rendered "for free" (§2).
    resumed_state = run_job(job_dir, progress=NullSink())
    assert resumed_state.status == "done"
    assert resumed_state.artifacts_done == resumed_state.artifacts_total == total_artifacts
    assert len(resumed_state.segments) == 2
    assert [s.reason for s in resumed_state.segments] == ["paused", "done"]

    resumed_run_dir = Path(resumed_state.run_dir)  # type: ignore[arg-type]
    assert resumed_state.run_id == baseline.run_id  # same config -> same deterministic run id.

    # Byte-identical to an uninterrupted run (manifest minus generated_at, plus
    # kg/*.jsonl and artifacts/** verbatim) — the acceptance in §6.
    resumed_manifest = json.loads((resumed_run_dir / "manifest.json").read_text())
    baseline_manifest = baseline.manifest.to_dict()
    assert structural_view(resumed_manifest) == structural_view(baseline_manifest)
    assert _blob(resumed_run_dir, ("kg",)) == _blob(baseline.run_dir, ("kg",))
    assert _blob(resumed_run_dir, ("artifacts",)) == _blob(baseline.run_dir, ("artifacts",))


def test_run_job_reports_unknown_error_as_failed_and_resumable(tmp_path: Path) -> None:
    # A cost ceiling far below the estimate makes CostCeilingExceeded deterministic
    # and keyless (no need to mock a pipeline failure).
    config = _config(tmp_path / "run")
    scale = config.scale.model_copy(update={"cost_ceiling_usd": 0.0})
    config = config.model_copy(update={"scale": scale})
    job_dir = _create_job(tmp_path / "jobs", config)

    state = run_job(job_dir, progress=NullSink())

    assert state.status == "failed"
    assert state.error is not None
    assert state.error["type"] == "CostCeilingExceeded"
    assert len(state.segments) == 1
    assert state.segments[0].reason == "failed"

    # Raising the ceiling (as the UI would, by editing config.json) and re-running
    # resumes rather than staying stuck.
    raised = config.model_copy(
        update={"scale": config.scale.model_copy(update={"cost_ceiling_usd": None})}
    )
    (job_dir / "config.json").write_text(
        json.dumps(raised.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    resumed = run_job(job_dir, progress=NullSink())
    assert resumed.status == "done"
    assert len(resumed.segments) == 2


def test_signal_handlers_are_restored_after_run_job(tmp_path: Path) -> None:
    import signal

    before = signal.getsignal(signal.SIGTERM)
    config = _config(tmp_path / "run")
    job_dir = _create_job(tmp_path / "jobs", config)
    run_job(job_dir, progress=NullSink())
    assert signal.getsignal(signal.SIGTERM) is before
