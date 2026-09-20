"""Progress events on the pipeline (``docs/EXPLORER_RUNS.md`` §3.1, §6)."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

from enterprise_sim.assembly import execute_run
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.jobs.progress import JsonlSink, NullSink, ProgressEvent
from enterprise_sim.world_builders import build_world


def _config(output_dir: Path, *, seed: int = 7) -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "output_dir": str(output_dir),
        }
    )


class _ListSink:
    """A thread-safe :class:`~enterprise_sim.jobs.progress.ProgressSink` that records events."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self.events.append(event)

    def of_kind(self, kind: str) -> list[ProgressEvent]:
        return [e for e in self.events if e.kind == kind]


def test_default_progress_is_none_and_run_is_unaffected(tmp_path: Path) -> None:
    # Passing nothing keeps execute_run's byte-identical contract (no progress hook).
    result = execute_run(_config(tmp_path))
    assert result.run_dir.is_dir()


def test_execute_run_emits_the_documented_event_kinds(tmp_path: Path) -> None:
    sink = _ListSink()
    execute_run(_config(tmp_path), progress=sink)

    kinds = {e.kind for e in sink.events}
    assert kinds == {"phase", "world_built", "scheduled", "estimate", "artifact", "done"}

    phases = [e.data["phase"] for e in sink.of_kind("phase")]
    assert phases == ["world", "schedule", "estimate", "render", "assemble", "done"]


def test_world_built_matches_layer_a_before_layer_b_c_mutate_it(tmp_path: Path) -> None:
    # "world_built" is emitted right after Layer A, before Layer B/C add their own
    # nodes/edges (scheduler-created + artifact nodes) to the same World object —
    # so it is compared against a pristine build_world(), not the final result.
    config = _config(tmp_path)
    baseline = build_world(config)

    sink = _ListSink()
    execute_run(config, progress=sink)

    [world_built] = sink.of_kind("world_built")
    assert world_built.data["nodes"] == baseline.node_count
    assert world_built.data["edges"] == baseline.edge_count
    assert world_built.data["departments"] == len(baseline.nodes_by_type("Department")) == 1
    assert world_built.data["scenarios"] == 1


def test_scheduled_and_estimate_match_the_corpus(tmp_path: Path) -> None:
    sink = _ListSink()
    result = execute_run(_config(tmp_path), progress=sink)

    [scheduled] = sink.of_kind("scheduled")
    assert scheduled.data["events"] == len(result.corpus.journal)
    assert scheduled.data["artifacts_total"] == len(result.corpus.artifacts)
    assert sum(s["artifacts"] for s in scheduled.data["scenarios"]) == len(result.corpus.artifacts)

    [estimate] = sink.of_kind("estimate")
    assert estimate.data["artifacts_total"] == len(result.corpus.artifacts)
    assert estimate.data["model"] == result.corpus.estimate.model  # type: ignore[union-attr]


def test_artifact_events_count_and_progress_match_the_corpus(tmp_path: Path) -> None:
    sink = _ListSink()
    result = execute_run(_config(tmp_path), progress=sink)

    artifact_events = sink.of_kind("artifact")
    assert len(artifact_events) == len(result.corpus.artifacts)

    # done/total are a monotonically increasing run-wide counter (max_concurrency
    # defaults to 8, but a small golden-sized config still yields one scenario, so
    # ordering is deterministic here too).
    done_values = sorted(e.data["done"] for e in artifact_events)
    assert done_values == list(range(1, len(result.corpus.artifacts) + 1))
    assert all(e.data["total"] == len(result.corpus.artifacts) for e in artifact_events)

    paths = {e.data["path"] for e in artifact_events}
    assert paths == {a.path for a in result.corpus.artifacts}

    # Every artifact is freshly rendered (no prior cache) on a first run.
    assert all(e.data["cached"] is False for e in artifact_events)
    for event in artifact_events:
        assert set(event.data["usage"]) == {"input_tokens", "cached_input_tokens", "output_tokens"}


def test_done_event_matches_the_result(tmp_path: Path) -> None:
    sink = _ListSink()
    result = execute_run(_config(tmp_path), progress=sink)

    done_events = sink.of_kind("done")
    assert done_events  # execute_run always emits exactly one "done".
    done = done_events[-1]
    assert done.data["run_id"] == result.run_id
    assert done.data["run_dir"] == str(result.run_dir)
    assert done.data["artifacts"] == len(result.corpus.artifacts)
    assert done.data["events"] == len(result.corpus.journal)
    assert done.data["cost_usd_segment"] == done.data["cost_usd_total"]


def test_rerender_is_all_cache_hits_on_a_shared_cache_dir(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    config = _config(tmp_path / "run").model_copy(
        update={
            "scale": _config(tmp_path / "run").scale.model_copy(
                update={"cache_dir": str(cache_dir)}
            )
        }
    )
    execute_run(config)  # first render populates the cache.

    sink = _ListSink()
    execute_run(config, progress=sink)  # same config/cache_dir: every call is a hit.
    artifact_events = sink.of_kind("artifact")
    assert artifact_events
    assert all(e.data["cached"] is True for e in artifact_events)


def test_null_sink_is_a_no_op() -> None:
    sink = NullSink()
    sink.emit(ProgressEvent.now("phase", phase="world"))  # must not raise.


def test_jsonl_sink_writes_to_stream_and_file(tmp_path: Path) -> None:
    also_to = tmp_path / "progress.jsonl"
    stream = io.StringIO()
    sink = JsonlSink(stream, also_to=also_to)
    try:
        sink.emit(ProgressEvent.now("phase", phase="world"))
        sink.emit(ProgressEvent.now("phase", phase="schedule"))
    finally:
        sink.close()

    stream_lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    file_lines = [json.loads(line) for line in also_to.read_text(encoding="utf-8").splitlines()]
    assert stream_lines == file_lines
    assert [line["data"]["phase"] for line in stream_lines] == ["world", "schedule"]


def test_jsonl_sink_is_thread_safe(tmp_path: Path) -> None:
    stream = io.StringIO()
    sink = JsonlSink(stream, also_to=tmp_path / "progress.jsonl")
    n = 200

    def worker(i: int) -> None:
        sink.emit(ProgressEvent.now("artifact", done=i, total=n))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sink.close()

    lines = stream.getvalue().splitlines()
    # Every emit produced exactly one well-formed, complete JSON line (no
    # interleaving from concurrent writers).
    assert len(lines) == n
    for line in lines:
        json.loads(line)
