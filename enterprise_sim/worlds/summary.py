"""One world, summarized for a list (``enterprise-sim world list`` / the explorer).

A flat, JSON-ready view of a world directory built from its ``.world`` plus two
cheap filesystem facts (is the KG exported, is the job's pid alive) — the one
shape every world list in the system renders from, so the CLI, the explorer's
picker and its Worlds tab can never disagree about what a world is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enterprise_sim.jobs.state import is_alive
from enterprise_sim.worlds.file import WorldFile, read_world

__all__ = ["summarize_world", "summarize_world_file"]

_KG_NODES = Path("kg") / "nodes.jsonl"


def summarize_world_file(world_dir: Path | str, world: WorldFile) -> dict[str, Any]:
    """Summarize an already-read ``.world`` (see :func:`summarize_world`)."""
    world_dir = Path(world_dir)
    job = world.job
    alive = is_alive(job.pid) if job.status == "running" else False
    # A "running" job whose process is gone was interrupted (killed, machine
    # rebooted, app closed hard): resumable, but not actually running.
    status = "interrupted" if job.status == "running" and not alive else job.status
    return {
        "world_id": world.world_id,
        "world_dir": str(world_dir),
        "schema_version": world.schema_version,
        "company": dict(world.company),
        "seed": world.seed,
        "window": dict(world.window),
        "counts": dict(world.counts),
        "render_backend": world.render_backend,
        "live": world.live,
        "config_digest": world.config_digest,
        "created_at": world.created_at,
        "updated_at": world.updated_at,
        "status": status,
        "alive": alive,
        "job": job.to_dict(),
        "has_kg": (world_dir / _KG_NODES).is_file(),
        "data": {name: _data_summary(entry) for name, entry in sorted(world.data.items())},
        "evals": dict(world.evals),
        "history": [
            {"kind": h.get("kind"), "at": h.get("at"), "changes": h.get("changes")}
            for h in world.history
        ],
        "lineage": dict(world.lineage) if world.lineage is not None else None,
    }


def summarize_world(world_dir: Path | str) -> dict[str, Any] | None:
    """Summarize the world at ``world_dir``, or ``None`` if it holds no ``.world``."""
    world = read_world(world_dir)
    if world is None:
        return None
    return summarize_world_file(world_dir, world)


def _data_summary(entry: dict[str, Any]) -> dict[str, Any]:
    """The headline of one structured-data scenario (title, rows, questions, cost)."""
    rows_by_table = entry.get("rows_by_table") or {}
    return {
        "title": entry.get("title"),
        "rows": sum(int(n) for n in rows_by_table.values()) if rows_by_table else 0,
        "tables": len(rows_by_table),
        "questions": dict(entry.get("questions") or {}),
        "iterations": len(entry.get("iterations") or []),
        "llm_cost_usd": entry.get("llm_cost_usd", 0.0),
        "created_at": entry.get("created_at"),
    }
