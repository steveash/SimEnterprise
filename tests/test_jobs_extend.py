"""Extending a world in place (``docs/EXPLORER_RUNS.md`` §3.5, §6).

Findings from exploring this against the actual pipeline (reported in full in
the implementing session, since the spec asked not to fake a guarantee that
does not hold): the "prefix preservation" reuse guarantee holds **exactly** for
a **pure window extension** (later ``period_end``, no added projects) — node
ids, *every* edge id (including LLM-chosen ``references`` edges), and cache
reuse are all a clean superset/match: every parent artifact comes back as a
cache hit, and the count of non-cached artifacts equals exactly
``new_total - old_total``.

It does **not** hold as cleanly when a project is *added*:
:class:`~enterprise_sim.producers.grounding.Roster` (D30 layer 1, the
"constrained input" block every producer's prompt includes) lists every
in-scope ``Project``/``Team``/``Initiative`` in the *whole company*, not just
the current scenario's — Layer A builds every config project before Layer B/C
runs, so a project added anywhere makes the roster block (and hence every
producer's prompt, for every scenario, not just the new project's) one line
longer than before. The fake backend's deterministic-per-prompt content then
differs, so:

* **node ids still superset cleanly** (structure — who/what exists — is
  unaffected);
* **structural edges** (``authored``/``reviewed``/``expresses``/``under``/…)
  **still superset cleanly** (templated from bound roles, D30 layer 2, never
  from the perturbed prose);
* **``references`` edges do not** — which prior artifacts a draft chooses to
  cite is read off the model's (here, the fake backend's) generated content,
  and that content changed with the prompt;
* consequently **no artifact comes back as a cache hit** either (every prompt
  changed), so the reuse-count guarantee does not apply.

Since an extend now happens *in place* — same folder, same world-local cache —
there is nothing to copy for the reuse guarantee; the tests assert the strict
reuse count and full node+edge superset for the window-only case, and for the
added-project case only what is actually true (node ids and structural edges
superset; the earlier cache entries are still on disk next to the new ones).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from enterprise_sim.assembly import (
    build_world_file,
    canonical_config,
    compute_config_digest,
    compute_world_id,
    execute_run,
)
from enterprise_sim.core.config import WorldConfig, load_config_from_mapping
from enterprise_sim.core.config.models import ProjectConfig
from enterprise_sim.jobs.extend import ExtendError, copy_cache, extend_config
from enterprise_sim.jobs.progress import NullSink
from enterprise_sim.jobs.worker import run_job
from enterprise_sim.worlds import WorldFile, cache_dir, read_world, update_world, write_world


def _config(worlds_root: Path, *, seed: int = 7) -> WorldConfig:
    return load_config_from_mapping(
        {
            "company": {"name": "Acme Corp", "vertical": "software", "size": "small"},
            "simulation": {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            "seed": seed,
            "worlds_root": str(worlds_root),
        }
    )


def _create_world(worlds_root: Path, config: WorldConfig) -> Path:
    """Mint a world directory the way ``world create --no-run`` does."""
    world_dir = worlds_root / compute_world_id(config)
    world_dir.mkdir(parents=True)
    write_world(world_dir, build_world_file(config))
    return world_dir


def _extend_in_place(world_dir: Path, child: WorldConfig, changes: dict[str, object]) -> None:
    """Adopt ``child`` into the world's ``.world`` the way ``world extend`` does (§3.5)."""

    def adopt(world: WorldFile) -> None:
        world.record_extend(
            config=canonical_config(child),
            config_digest=compute_config_digest(child),
            changes=changes,
            at=datetime.now(UTC).isoformat(),
        )
        world.window["end"] = child.simulation.period_end.isoformat()
        world.job.status = "created"

    update_world(world_dir, adopt)


def _cache_entries(world_dir: Path) -> set[str]:
    return {p.name for p in cache_dir(world_dir).iterdir() if p.is_file()}


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


# --- the fork's cache copy -----------------------------------------------------


def test_copy_cache_is_copy_not_move_and_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("1", encoding="utf-8")
    dst = tmp_path / "dst"

    copy_cache(src, dst)
    assert (dst / "a.json").read_text(encoding="utf-8") == "1"
    assert (src / "a.json").is_file()  # the source is untouched.

    # A second call is a no-op: it must not clobber entries the copy has since
    # grown past the source's.
    (dst / "b.json").write_text("2", encoding="utf-8")
    copy_cache(src, dst)
    assert (dst / "b.json").is_file()

    copy_cache(None, dst)  # no source cache: a no-op, must not raise.
    copy_cache(tmp_path / "missing", dst)


# --- end-to-end extend in place ------------------------------------------------


def test_extend_in_place_reuses_the_cache_exactly_for_a_pure_window_extension(
    tmp_path: Path,
) -> None:
    # A wide window growth (§6): every earlier artifact must come back as a cache
    # hit, and the count of non-cached artifacts must equal exactly
    # new_total - old_total — the reuse guarantee the doc asks tests to lock.
    parent_config = _config(tmp_path / "unused")
    world_dir = _create_world(tmp_path / "worlds", parent_config)
    parent_state = run_job(world_dir, progress=NullSink())
    assert parent_state.status == "done"
    parent_total = parent_state.artifacts_total
    parent_entries = _cache_entries(world_dir)
    assert parent_entries

    new_end = parent_config.simulation.period_end + timedelta(days=60)
    child_config = extend_config(parent_config, period_end=new_end)
    _extend_in_place(
        world_dir,
        child_config,
        {"period_end": [parent_config.simulation.period_end.isoformat(), new_end.isoformat()]},
    )
    child_state = run_job(world_dir, progress=NullSink())

    assert child_state.status == "done"
    assert child_state.artifacts_total > parent_total
    assert child_state.artifacts_cached == parent_total
    non_cached = child_state.artifacts_total - child_state.artifacts_cached
    assert non_cached == child_state.artifacts_total - parent_total
    assert [s.reason for s in child_state.segments] == ["done", "done"]
    assert child_state.cost_usd_total == sum(s.cost_usd for s in child_state.segments)

    world = read_world(world_dir)
    assert world is not None
    assert world.world_id == compute_world_id(parent_config)  # the id never changes.
    assert [h["kind"] for h in world.history] == ["create", "extend"]
    assert world.history[-1]["changes"]["period_end"] == [
        parent_config.simulation.period_end.isoformat(),
        new_end.isoformat(),
    ]
    assert world.window["end"] == new_end.isoformat()
    assert parent_entries <= _cache_entries(world_dir)  # nothing was evicted.


def test_extend_pure_window_world_is_a_full_node_and_edge_superset(tmp_path: Path) -> None:
    # A wide, project-free window extension: node ids AND every edge id
    # (including LLM-chosen `references` edges) superset cleanly (see module
    # docstring) — the strongest form of the guarantee, measured to actually hold.
    parent_config = _config(tmp_path / "worlds")
    parent_result = execute_run(parent_config)

    new_end = parent_config.simulation.period_end + timedelta(days=60)
    child_config = extend_config(parent_config, period_end=new_end)
    child_result = execute_run(child_config, world_dir=parent_result.world_dir)  # in place
    assert child_result.world_dir == parent_result.world_dir
    assert child_result.world_id == parent_result.world_id

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
    parent_config = _config(tmp_path / "worlds")
    parent_result = execute_run(parent_config)

    new_end = parent_config.simulation.period_end + timedelta(days=5)
    child_config = extend_config(
        parent_config,
        period_end=new_end,
        add_projects=(ProjectConfig(name="Extra Widget", description="more work"),),
    )
    child_result = execute_run(child_config, world_dir=parent_result.world_dir)  # in place

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


def test_extend_with_added_project_keeps_the_earlier_cache_entries(tmp_path: Path) -> None:
    # As found above: adding a project changes every scenario's roster block, so
    # no artifact comes back as a cache hit. What *is* still true, and is what
    # this asserts: the world-local cache keeps the earlier entries next to the
    # new ones (nothing is evicted or copied), so a later window-only extend
    # still benefits from them.
    parent_config = _config(tmp_path / "unused")
    world_dir = _create_world(tmp_path / "worlds", parent_config)
    assert run_job(world_dir, progress=NullSink()).status == "done"
    parent_entries = _cache_entries(world_dir)
    assert parent_entries

    child_config = extend_config(parent_config, add_projects=(ProjectConfig(name="Extra Widget"),))
    _extend_in_place(world_dir, child_config, {"added_projects": ["Extra Widget"]})
    child_state = run_job(world_dir, progress=NullSink())
    assert child_state.status == "done"

    child_entries = _cache_entries(world_dir)
    assert parent_entries <= child_entries
    assert child_entries > parent_entries  # the new prompts were rendered and cached too.
