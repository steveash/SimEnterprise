"""Cooperative pause/resume, the cost ledger across segments, and world portability.

``EXPLORER_RUNS.md`` §3.2/§6, plus the property the one-folder-one-``.world``
layout exists for: a paused world can be zipped, moved and resumed elsewhere
with every already-rendered artifact still a free cache hit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from enterprise_sim.assembly import (
    build_world_file,
    canonical_config,
    compute_world_id,
    execute_run,
)
from enterprise_sim.core.config import WorldConfig, load_config_from_mapping
from enterprise_sim.jobs.control import RunControl
from enterprise_sim.jobs.progress import NullSink, ProgressEvent
from enterprise_sim.jobs.worker import run_job
from enterprise_sim.worlds import (
    WorldFile,
    cache_dir,
    is_world_dir,
    read_world,
    structural_view,
    update_world,
    write_world,
)


def _config(worlds_root: Path, *, seed: int = 7) -> WorldConfig:
    config = load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "worlds_root": str(worlds_root),
        }
    )
    # Deterministic exact-N pause counts need one artifact rendered at a time
    # (§6: "Use max_concurrency = 1 in that test"). No cache_dir: the cache is
    # world-local, which is what makes the world portable.
    scale = config.scale.model_copy(update={"max_concurrency": 1})
    return config.model_copy(update={"scale": scale})


def _create_world(worlds_root: Path, config: WorldConfig) -> Path:
    """Mint a world directory the way ``world create --no-run`` does."""
    world_dir = worlds_root / compute_world_id(config)
    world_dir.mkdir(parents=True)
    write_world(world_dir, build_world_file(config))
    return world_dir


class _PauseAfterN:
    """A progress sink that requests a pause once N ``artifact`` events were seen."""

    def __init__(self, world_dir: Path, n: int) -> None:
        self._world_dir = world_dir
        self._n = n
        self._count = 0
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)
        if event.kind == "artifact":
            self._count += 1
            if self._count == self._n:
                RunControl(self._world_dir).request_pause()


def _blob(world_dir: Path, subdirs: tuple[str, ...]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for sub in subdirs:
        for path in sorted((world_dir / sub).rglob("*")):
            if path.is_file():
                out[str(path.relative_to(world_dir))] = path.read_bytes()
    return out


def _comparable(world: WorldFile) -> dict[str, object]:
    """The structural view minus ``evals`` (a job generates the eval set; a bare build doesn't)."""
    return {k: v for k, v in structural_view(world.to_dict()).items() if k != "evals"}


def test_worker_run_job_completes_uninterrupted(tmp_path: Path) -> None:
    config = _config(tmp_path / "unused")
    world_dir = _create_world(tmp_path / "worlds", config)

    state = run_job(world_dir, progress=NullSink())

    assert state.status == "done"
    assert is_world_dir(world_dir)
    assert state.artifacts_done == state.artifacts_total > 0
    assert len(state.segments) == 1
    assert state.segments[0].reason == "done"
    assert state.cost_usd_total == state.cost_usd_segment == state.segments[0].cost_usd
    on_disk = read_world(world_dir)
    assert on_disk is not None
    assert on_disk.job == state
    assert on_disk.counts["nodes"] > 0  # the build's own fields landed in the same file.


def test_pause_mid_render_then_resume_matches_uninterrupted_run(tmp_path: Path) -> None:
    # A control-free build of the same config to learn the total artifact count
    # (so the pause point is a real mid-render point, not past the end) and to
    # have something to diff the resumed world against.
    baseline = execute_run(_config(tmp_path / "baseline"))
    total_artifacts = len(baseline.corpus.artifacts)
    assert total_artifacts >= 3, "need multiple artifacts for a meaningful mid-render pause"
    pause_after = total_artifacts - 1

    config = _config(tmp_path / "unused")
    world_dir = _create_world(tmp_path / "worlds", config)
    sink = _PauseAfterN(world_dir, pause_after)

    paused_state = run_job(world_dir, progress=sink)

    assert paused_state.status == "paused"
    # max_concurrency=1 (§6) makes this an exact count, not a range.
    assert paused_state.artifacts_done == pause_after
    assert paused_state.artifacts_total == total_artifacts
    assert len(paused_state.segments) == 1
    assert paused_state.segments[0].reason == "paused"
    paused_events = [e for e in sink.events if e.kind == "paused"]
    assert len(paused_events) == 1
    assert paused_events[0].data == {"done": pause_after, "total": total_artifacts}

    # Resume: run_job is idempotent — it re-runs the full pipeline; the world-local
    # response cache serves whatever already rendered "for free" (§2).
    resumed_state = run_job(world_dir, progress=NullSink())
    assert resumed_state.status == "done"
    assert resumed_state.artifacts_done == resumed_state.artifacts_total == total_artifacts
    assert resumed_state.artifacts_cached == pause_after
    assert len(resumed_state.segments) == 2
    assert [s.reason for s in resumed_state.segments] == ["paused", "done"]
    resumed = read_world(world_dir)
    assert resumed is not None
    assert resumed.world_id == baseline.world_id  # same config -> same deterministic id.

    # Byte-identical to an uninterrupted build (.world minus its volatile fields,
    # plus kg/*.jsonl and artifacts/** verbatim) — the acceptance in §6.
    assert _comparable(resumed) == _comparable(baseline.world_file)
    assert _blob(world_dir, ("kg",)) == _blob(baseline.world_dir, ("kg",))
    assert _blob(world_dir, ("artifacts",)) == _blob(baseline.world_dir, ("artifacts",))


def test_a_paused_world_resumes_after_being_moved(tmp_path: Path) -> None:
    # The point of one-folder-one-.world: zip it up, move it, unzip it, and
    # everything — the ledger, the config, the response cache — still works.
    baseline_total = len(execute_run(_config(tmp_path / "baseline")).corpus.artifacts)
    pause_after = baseline_total - 1

    config = _config(tmp_path / "unused")
    world_dir = _create_world(tmp_path / "worlds", config)
    paused = run_job(world_dir, progress=_PauseAfterN(world_dir, pause_after))
    assert paused.status == "paused"
    assert cache_dir(world_dir).is_dir()

    moved = tmp_path / "elsewhere" / "the-same-world"
    moved.parent.mkdir()
    shutil.move(str(world_dir), str(moved))
    assert not world_dir.exists()

    resumed = run_job(moved, progress=NullSink())
    assert resumed.status == "done"
    assert resumed.artifacts_total == baseline_total
    assert resumed.artifacts_cached == pause_after  # nothing was re-billed.
    assert [s.reason for s in resumed.segments] == ["paused", "done"]
    on_disk = read_world(moved)
    assert on_disk is not None
    assert on_disk.world_id == compute_world_id(config)  # identity travels with the folder.
    assert on_disk.job.status == "done"


def test_run_job_reports_unknown_error_as_failed_and_resumable(tmp_path: Path) -> None:
    # A cost ceiling far below the estimate makes CostCeilingExceeded deterministic
    # and keyless (no need to mock a pipeline failure).
    config = _config(tmp_path / "unused")
    scale = config.scale.model_copy(update={"cost_ceiling_usd": 0.0})
    config = config.model_copy(update={"scale": scale})
    world_dir = _create_world(tmp_path / "worlds", config)

    state = run_job(world_dir, progress=NullSink())

    assert state.status == "failed"
    assert state.error is not None
    assert state.error["type"] == "CostCeilingExceeded"
    assert len(state.segments) == 1
    assert state.segments[0].reason == "failed"

    # Raising the ceiling (as the UI would, via `world configure`) and re-running
    # resumes rather than staying stuck.
    raised = config.model_copy(
        update={"scale": config.scale.model_copy(update={"cost_ceiling_usd": None})}
    )

    def patch(world: WorldFile) -> None:
        world.config = canonical_config(raised)

    update_world(world_dir, patch)
    resumed = run_job(world_dir, progress=NullSink())
    assert resumed.status == "done"
    assert resumed.error is None
    assert len(resumed.segments) == 2


def test_signal_handlers_are_restored_after_run_job(tmp_path: Path) -> None:
    import signal

    before = signal.getsignal(signal.SIGTERM)
    world_dir = _create_world(tmp_path / "worlds", _config(tmp_path / "unused"))
    run_job(world_dir, progress=NullSink())
    assert signal.getsignal(signal.SIGTERM) is before
