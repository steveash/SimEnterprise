"""Extending a finished run (``docs/EXPLORER_RUNS.md`` §3.5, §6).

Findings from exploring this against the actual pipeline (reported in full in
the implementing session, since the spec asked not to fake a guarantee that
does not hold): the "prefix preservation" reuse guarantee holds **exactly** for
a **pure window extension** (later ``period_end``, no added projects) — node
ids, *every* edge id (including LLM-chosen ``references`` edges), and cache
reuse are all a clean superset/match: every parent artifact comes back as a
cache hit, and the count of non-cached child artifacts equals exactly
``child_total - parent_total``.

It does **not** hold as cleanly when a project is *added*:
:class:`~enterprise_sim.producers.grounding.Roster` (D30 layer 1, the
"constrained input" block every producer's prompt includes) lists every
in-scope ``Project``/``Team``/``Initiative`` in the *whole company*, not just
the current scenario's — Layer A builds every config project before Layer B/C
runs, so a project added anywhere makes the roster block (and hence every
producer's prompt, for every scenario, not just the new project's) one line
longer than the parent's. The fake backend's deterministic-per-prompt content
then differs, so:

* **node ids still superset cleanly** (structure — who/what exists — is
  unaffected);
* **structural edges** (``authored``/``reviewed``/``expresses``/``under``/…)
  **still superset cleanly** (templated from bound roles, D30 layer 2, never
  from the perturbed prose);
* **``references`` edges do not** — which prior artifacts a draft chooses to
  cite is read off the model's (here, the fake backend's) generated content,
  and that content changed with the prompt. In the measured case this loses
  30 of the parent's 59 ``references`` edges;
* consequently **no artifact comes back as a cache hit** either (every prompt
  in the run changed), so the reuse-count guarantee does not apply.

This is exactly the kind of "a producer's prompt legitimately changes" case
this module's own doc anticipates — the tests below just measured *which*
change triggers it (company-wide roster scope, not the window growing, which
turned out to be prefix-clean) and assert accordingly: the strict reuse count
and full node+edge superset for the window-only case, and — for the
added-project case — only what is actually true (node ids and structural edges
superset; the parent's cache entries are genuinely present on disk in the
child's cache dir, seeded by copy-on-start, even though this run's prompts
don't match them).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from enterprise_sim.core.config import RunConfig, load_config_from_mapping
from enterprise_sim.core.config.models import ProjectConfig
from enterprise_sim.jobs.extend import (
    ExtendError,
    copy_cache_on_start,
    extend_config,
    find_parent_job_dir,
    resolve_parent_cache_dir,
)
from enterprise_sim.jobs.progress import NullSink
from enterprise_sim.jobs.state import JobState, write_state
from enterprise_sim.jobs.worker import run_job


def _config(output_dir: Path, *, seed: int = 7) -> RunConfig:
    return load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "output_dir": str(output_dir),
        }
    )


def _create_job(
    jobs_root: Path,
    config: RunConfig,
    *,
    job_id: str,
    kind: str = "new",
    parent_run_dir: Path | str | None = None,
    changes: dict[str, object] | None = None,
) -> Path:
    """Write a minimal job directory the way ``job create`` would (§3.3/§3.5)."""
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
        "kind": kind,
        "live": False,
        "config": config.model_dump(mode="json"),
        "parent_run_dir": str(parent_run_dir) if parent_run_dir is not None else None,
        "changes": changes,
        "created_at": now,
    }
    (job_dir / "job.json").write_text(json.dumps(job_payload, sort_keys=True), encoding="utf-8")
    (job_dir / "config.json").write_text(
        json.dumps(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    write_state(job_dir, JobState(status="created", started_at=now))
    return job_dir


# --- extend_config itself ---------------------------------------------------


def test_extend_config_moves_period_end_later(tmp_path: Path) -> None:
    parent = _config(tmp_path)
    new_end = parent.simulation.period_end + timedelta(days=5)
    child = extend_config(parent, period_end=new_end)
    assert child.simulation.period_end == new_end
    assert child.simulation.period_start == parent.simulation.period_start
    assert child.seed == parent.seed
    assert child.company == parent.company


def test_extend_config_rejects_an_earlier_period_end(tmp_path: Path) -> None:
    parent = _config(tmp_path)
    earlier = parent.simulation.period_end - timedelta(days=1)
    with pytest.raises(ExtendError):
        extend_config(parent, period_end=earlier)


def test_extend_config_appends_projects_after_the_parents(tmp_path: Path) -> None:
    parent = _config(tmp_path).model_copy(update={"projects": (ProjectConfig(name="Original"),)})
    added = (ProjectConfig(name="New One"),)
    child = extend_config(parent, add_projects=added)
    assert child.projects == (*parent.projects, *added)


def test_extend_config_is_a_no_op_when_nothing_changes(tmp_path: Path) -> None:
    parent = _config(tmp_path)
    assert extend_config(parent) is parent
    assert extend_config(parent, period_end=parent.simulation.period_end) is parent


# --- cache resolution --------------------------------------------------------


def test_resolve_parent_cache_dir_finds_the_parent_job(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    parent_dir = jobs_root / "parent-job"
    parent_dir.mkdir(parents=True)
    (parent_dir / "llm-cache").mkdir()
    write_state(parent_dir, JobState(status="done", run_dir=str(tmp_path / "runs" / "parent-run")))

    resolved = resolve_parent_cache_dir(tmp_path / "runs" / "parent-run", jobs_root=jobs_root)
    assert resolved == parent_dir / "llm-cache"
    assert find_parent_job_dir(jobs_root, tmp_path / "runs" / "parent-run") == parent_dir


def test_resolve_parent_cache_dir_falls_back_to_the_run_snapshot(tmp_path: Path) -> None:
    run_dir = tmp_path / "bare-run"
    run_dir.mkdir()
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"scale": {"cache_dir": str(tmp_path / "some-cache")}}), encoding="utf-8"
    )
    # No jobs_root: the parent was a bare `enterprise-sim run`, not a job.
    assert resolve_parent_cache_dir(run_dir) == tmp_path / "some-cache"


def test_resolve_parent_cache_dir_is_none_with_nothing_to_find(tmp_path: Path) -> None:
    run_dir = tmp_path / "bare-run"
    run_dir.mkdir()
    (run_dir / "config.snapshot.json").write_text(json.dumps({"scale": {}}), encoding="utf-8")
    assert resolve_parent_cache_dir(run_dir) is None


def test_copy_cache_on_start_is_copy_not_move_and_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("1", encoding="utf-8")
    dst = tmp_path / "dst"

    copy_cache_on_start(src, dst)
    assert (dst / "a.json").read_text(encoding="utf-8") == "1"
    assert (src / "a.json").is_file()  # the source is untouched.

    # A second call (as a later resume would make) is a no-op: it must not
    # clobber entries the child has since grown past the parent's.
    (dst / "b.json").write_text("2", encoding="utf-8")
    copy_cache_on_start(src, dst)
    assert (dst / "b.json").is_file()

    copy_cache_on_start(None, dst)  # no parent cache to copy from: a no-op, must not raise.


# --- end-to-end extend job ---------------------------------------------------


def test_extend_reuses_the_parent_cache_exactly_for_a_pure_window_extension(
    tmp_path: Path,
) -> None:
    # A wide window growth (§6): every parent artifact must come back as a cache
    # hit, and the count of non-cached child artifacts must equal exactly
    # child_total - parent_total — the reuse guarantee the doc asks tests to lock.
    parent_config = _config(tmp_path / "parent")
    jobs_root = tmp_path / "jobs"
    parent_job_dir = _create_job(jobs_root, parent_config, job_id="parent-job")
    parent_state = run_job(parent_job_dir, progress=NullSink())
    assert parent_state.status == "done"
    assert parent_state.run_dir is not None

    new_end = parent_config.simulation.period_end + timedelta(days=60)
    child_config = extend_config(parent_config, period_end=new_end)
    child_job_dir = _create_job(
        jobs_root,
        child_config,
        job_id="child-job",
        kind="extend",
        parent_run_dir=parent_state.run_dir,
        changes={
            "period_end": [
                parent_config.simulation.period_end.isoformat(),
                child_config.simulation.period_end.isoformat(),
            ],
            "added_projects": [],
        },
    )
    child_state = run_job(child_job_dir, progress=NullSink())

    assert child_state.status == "done"
    assert child_state.artifacts_total > parent_state.artifacts_total
    assert child_state.artifacts_cached == parent_state.artifacts_total
    non_cached = child_state.artifacts_total - child_state.artifacts_cached
    assert non_cached == child_state.artifacts_total - parent_state.artifacts_total

    lineage = json.loads((Path(child_state.run_dir) / "lineage.json").read_text())  # type: ignore[arg-type]
    assert lineage["parent_run_id"] == Path(parent_state.run_dir).name
    assert lineage["parent_run_dir"] == str(parent_state.run_dir)
    assert lineage["changes"]["period_end"] == [
        parent_config.simulation.period_end.isoformat(),
        child_config.simulation.period_end.isoformat(),
    ]


def test_extend_pure_window_world_is_a_full_node_and_edge_superset(tmp_path: Path) -> None:
    # A wide, project-free window extension: node ids AND every edge id
    # (including LLM-chosen `references` edges) superset cleanly (see module
    # docstring) — the strongest form of the guarantee, measured to actually hold.
    from enterprise_sim.assembly import execute_run

    parent_config = _config(tmp_path / "parent")
    parent_result = execute_run(parent_config)

    new_end = parent_config.simulation.period_end + timedelta(days=60)
    child_config = extend_config(parent_config, period_end=new_end)
    child_result = execute_run(child_config)

    parent_node_ids = {n.id for n in parent_result.world.nodes()}
    parent_edge_ids = {e.id for e in parent_result.world.edges()}
    child_node_ids = {n.id for n in child_result.world.nodes()}
    child_edge_ids = {e.id for e in child_result.world.edges()}

    assert parent_node_ids
    assert parent_edge_ids
    assert parent_node_ids <= child_node_ids
    assert parent_edge_ids <= child_edge_ids
    assert child_node_ids > parent_node_ids  # the extra window really added something.


def test_extend_with_added_project_world_is_a_structural_superset(tmp_path: Path) -> None:
    # Adding a project (module docstring): node ids and *structural* edges
    # (everything but the LLM-content-derived `references` edges) still superset
    # cleanly; `references` edges are not asserted here because they measurably
    # do not (the roster perturbation changes every scenario's generated prose).
    from enterprise_sim.assembly import execute_run

    parent_config = _config(tmp_path / "parent")
    parent_result = execute_run(parent_config)

    new_end = parent_config.simulation.period_end + timedelta(days=5)
    child_config = extend_config(
        parent_config,
        period_end=new_end,
        add_projects=(ProjectConfig(name="Extra Widget", description="more work"),),
    )
    child_result = execute_run(child_config)

    parent_node_ids = {n.id for n in parent_result.world.nodes()}
    child_node_ids = {n.id for n in child_result.world.nodes()}
    assert parent_node_ids <= child_node_ids

    parent_structural_edges = {e.id for e in parent_result.world.edges() if e.type != "references"}
    child_structural_edges = {e.id for e in child_result.world.edges() if e.type != "references"}
    assert parent_structural_edges
    assert parent_structural_edges <= child_structural_edges

    # The new project is really there (not just an id-superset coincidence).
    assert child_result.world.get_node("project:extra-widget") is not None
    assert parent_result.world.get_node("project:extra-widget") is None


def test_extend_with_added_project_still_copies_the_parent_cache_on_disk(
    tmp_path: Path,
) -> None:
    # As found above: adding a project changes every scenario's roster block, so
    # the fake backend's deterministic content differs and no artifact in this
    # child comes back as a cache hit. What *is* still true, and is what this
    # asserts: copy-on-start really did seed the child's cache dir with the
    # parent's entries (the mechanism works; this run's prompts just don't match
    # them) — the reuse guarantee's grounding, without overclaiming its effect.
    parent_config = _config(tmp_path / "parent")
    jobs_root = tmp_path / "jobs"
    parent_job_dir = _create_job(jobs_root, parent_config, job_id="parent-job")
    parent_state = run_job(parent_job_dir, progress=NullSink())
    assert parent_state.status == "done"
    parent_cache_dir = parent_job_dir / "llm-cache"
    parent_cache_entries = {p.name for p in parent_cache_dir.iterdir() if p.is_file()}
    assert parent_cache_entries

    child_config = extend_config(parent_config, add_projects=(ProjectConfig(name="Extra Widget"),))
    child_job_dir = _create_job(
        jobs_root,
        child_config,
        job_id="child-job",
        kind="extend",
        parent_run_dir=parent_state.run_dir,
    )
    child_state = run_job(child_job_dir, progress=NullSink())
    assert child_state.status == "done"

    child_cache_dir = child_job_dir / "llm-cache"
    child_cache_entries = {p.name for p in child_cache_dir.iterdir() if p.is_file()}
    assert parent_cache_entries <= child_cache_entries
