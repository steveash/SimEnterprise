"""Deriving a world's next config: extend in place, or fork a parallel world (§3.5).

``extend_config`` derives a child :class:`WorldConfig` from a parent's: a later
``period_end`` and/or more scenario instances (projects). ``world extend``
applies it to the *same* world directory — the ``.world`` adopts the config,
keeps its ``world_id``, and the next ``world run`` re-renders with every
already-rendered artifact a free hit in the world-local response cache, so the
reuse guarantee needs no copying at all. ``world fork`` makes a *new* world
from the derived config (with ``lineage`` back to the parent) and seeds its
cache with :func:`copy_cache` so the fork reuses the parent's work too.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from enterprise_sim.core.config import WorldConfig
from enterprise_sim.core.config.models import ProjectConfig

__all__ = ["ExtendError", "copy_cache", "extend_config"]


class ExtendError(ValueError):
    """An extension that would shrink or otherwise break the parent's world."""


def extend_config(
    parent: WorldConfig,
    *,
    period_end: date | None = None,
    add_projects: Sequence[ProjectConfig] = (),
) -> WorldConfig:
    """Derive a child config from ``parent`` (§3.5): more time and/or more instances.

    * **more time**: ``period_end`` may only move later than the parent's (a
      shorter window would drop already-rendered events); equal is a no-op.
    * **more instances**: ``add_projects`` are appended after the parent's own
      so the parent's project ids and ordering are preserved.

    Everything else (company, seed, departments, model, scale) is inherited, so
    Layer A reproduces the same organization the parent's did. Returns ``parent``
    itself when nothing changes.
    """
    updates: dict[str, object] = {}
    if period_end is not None and period_end != parent.simulation.period_end:
        if period_end < parent.simulation.period_end:
            raise ExtendError(
                f"period_end ({period_end.isoformat()}) must not precede the "
                f"parent's ({parent.simulation.period_end.isoformat()})"
            )
        updates["simulation"] = parent.simulation.model_copy(update={"period_end": period_end})
    if add_projects:
        updates["projects"] = (*parent.projects, *add_projects)
    if not updates:
        return parent
    return parent.model_copy(update=updates)


def copy_cache(src: Path | None, dst: Path) -> None:
    """Seed ``dst`` with the response-cache entries of ``src`` (a fork's copy-on-create).

    A no-op when ``src`` is ``None``/missing, or when ``dst`` already has entries
    — so it never clobbers a cache that has since grown past the source's.
    Entries are individual :class:`~enterprise_sim.core.llm.cache.ResponseCache`
    JSON files; they are copied, not moved, so the source is left untouched.
    """
    if src is None or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    if any(dst.iterdir()):
        return
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)
