"""Extending a finished run with more time or more instances (``docs/EXPLORER_RUNS.md`` §3.5).

``extend_config`` derives a child :class:`RunConfig` from a parent's: a later
``period_end`` and/or appended ``[[projects]]``, with everything else (seed,
company, model) inherited verbatim so Layer A reproduces the same org. The rest
of this module resolves the parent's response-cache directory and copies it
into the child's own cache *on start* — so a child job's render reuses whatever
the parent already rendered instead of re-billing it (§3.5's reuse guarantee).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from enterprise_sim.core.config.models import DepartmentConfig, ProjectConfig, RunConfig
from enterprise_sim.jobs.state import read_state

__all__ = [
    "ExtendError",
    "copy_cache_on_start",
    "extend_config",
    "find_parent_job_dir",
    "resolve_parent_cache_dir",
]


class ExtendError(Exception):
    """Raised when an extend request is invalid (e.g. ``period_end`` moves earlier)."""


def extend_config(
    parent: RunConfig,
    *,
    period_end: date | None = None,
    add_projects: Sequence[ProjectConfig] = (),
    departments: Sequence[DepartmentConfig] | None = None,
) -> RunConfig:
    """Derive a child config from ``parent`` (§3.5): more time and/or more instances.

    * **more time**: ``period_end`` may only move later than the parent's (a
      :class:`ExtendError` names the violation otherwise); ``None`` keeps it.
    * **more instances**: ``add_projects`` are appended after the parent's own
      ``projects``, which are kept verbatim, in order.
    * ``departments`` replaces the department selection outright when given
      (``None`` keeps the parent's); it is not merged, since a department list is
      an explicit, ordered choice (§3.6), not an accretive one.

    Everything else (seed, company, model, scale) is inherited unchanged, so
    Layer A reproduces the same organization the parent's did. Returns ``parent``
    itself (no-op) when nothing was asked to change.
    """
    updates: dict[str, object] = {}
    if period_end is not None and period_end != parent.simulation.period_end:
        if period_end < parent.simulation.period_end:
            raise ExtendError(
                f"extend: period_end ({period_end.isoformat()}) must not precede the "
                f"parent's ({parent.simulation.period_end.isoformat()})"
            )
        updates["simulation"] = parent.simulation.model_copy(update={"period_end": period_end})
    if add_projects:
        updates["projects"] = (*parent.projects, *add_projects)
    if departments is not None:
        updates["departments"] = tuple(departments)
    if not updates:
        return parent
    return parent.model_copy(update=updates)


def find_parent_job_dir(jobs_root: Path, parent_run_dir: Path) -> Path | None:
    """Find the job under ``jobs_root`` whose recorded ``run_dir`` is ``parent_run_dir``.

    Returns ``None`` when ``jobs_root`` does not exist or no job matches — the
    parent run may simply have been produced by a bare ``enterprise-sim run``,
    never wrapped in a job.
    """
    jobs_root = Path(jobs_root)
    if not jobs_root.is_dir():
        return None
    target = Path(parent_run_dir).resolve()
    for job_dir in sorted(jobs_root.iterdir()):
        if not job_dir.is_dir():
            continue
        state = read_state(job_dir)
        if state is not None and state.run_dir is not None:
            if Path(state.run_dir).resolve() == target:
                return job_dir
    return None


def resolve_parent_cache_dir(parent_run_dir: Path, *, jobs_root: Path | None = None) -> Path | None:
    """Resolve the parent's response-cache dir (§3.5).

    The parent job's ``llm-cache/`` if the parent run was produced by a job under
    ``jobs_root``, else the parent config's ``scale.cache_dir`` (read from its
    ``config.snapshot.json``) if it set one. ``None`` when neither is found —
    a parent rendered with no on-disk cache has nothing to reuse.
    """
    if jobs_root is not None:
        job_dir = find_parent_job_dir(jobs_root, parent_run_dir)
        if job_dir is not None:
            cache_dir = job_dir / "llm-cache"
            if cache_dir.is_dir():
                return cache_dir
    snapshot = Path(parent_run_dir) / "config.snapshot.json"
    if snapshot.is_file():
        try:
            data = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        cache_dir_value = data.get("scale", {}).get("cache_dir")
        if isinstance(cache_dir_value, str) and cache_dir_value:
            return Path(cache_dir_value)
    return None


def copy_cache_on_start(src: Path | None, dst: Path) -> None:
    """Copy-on-start: seed the child's cache dir with the parent's entries (§3.5).

    A no-op when ``src`` is ``None``/missing, or when ``dst`` already has entries
    — so a resumed child job never re-copies over cache entries it has already
    grown past the parent's. Entries are individual
    :class:`~enterprise_sim.core.llm.cache.ResponseCache` JSON files; they are
    copied, not moved, so the parent's cache is left untouched.
    """
    if src is None or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    if any(dst.iterdir()):
        return
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)
