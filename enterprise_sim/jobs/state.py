"""The job ledger: ``JobState`` + segments (``docs/EXPLORER_RUNS.md`` §3.3).

A world's ledger lives in its ``.world`` under ``job`` (see
:mod:`enterprise_sim.worlds.file`, whose :func:`~enterprise_sim.worlds.file.read_job_state`
/ :func:`~enterprise_sim.worlds.file.write_job_state` are the I/O). This module
is a leaf — the dataclasses and a pid liveness check only — so ``worlds`` can
depend on it without a cycle.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["JobState", "Segment", "is_alive"]


@dataclass(slots=True)
class Segment:
    """One render attempt: a ``world run`` invocation from start to pause/done/failure."""

    started_at: str
    ended_at: str | None = None
    artifacts_rendered: int = 0
    cost_usd: float = 0.0
    #: ``"paused"`` | ``"done"`` | ``"failed"`` | ``"killed"``.
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping this segment serializes to."""
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "artifacts_rendered": self.artifacts_rendered,
            "cost_usd": self.cost_usd,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Segment:
        """Reconstruct a :class:`Segment` from :meth:`to_dict` output."""
        return cls(
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
            artifacts_rendered=int(data.get("artifacts_rendered", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            reason=data.get("reason"),
        )


@dataclass(slots=True)
class JobState:
    """The ``job`` block of a ``.world`` (``docs/EXPLORER_RUNS.md`` §3.3's table).

    ``status`` is one of ``"created"`` / ``"running"`` / ``"paused"`` /
    ``"failed"`` / ``"done"`` / ``"cancelled"``. A job whose status is
    ``"running"`` but whose ``pid`` is dead (see :func:`is_alive`) is shown by a
    UI as *interrupted* (resumable) rather than actually running. The ledger
    spans the world's whole life: every create / extend / resume appends a
    segment, and ``cost_usd_total`` is the sum across all of them.
    """

    status: str = "created"
    pid: int | None = None
    phase: str | None = None
    artifacts_done: int = 0
    artifacts_total: int = 0
    artifacts_cached: int = 0
    estimate: dict[str, Any] | None = None
    cost_usd_total: float = 0.0
    cost_usd_segment: float = 0.0
    usage_total: dict[str, int] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping this state serializes to."""
        return {
            "status": self.status,
            "pid": self.pid,
            "phase": self.phase,
            "artifacts_done": self.artifacts_done,
            "artifacts_total": self.artifacts_total,
            "artifacts_cached": self.artifacts_cached,
            "estimate": self.estimate,
            "cost_usd_total": self.cost_usd_total,
            "cost_usd_segment": self.cost_usd_segment,
            "usage_total": dict(self.usage_total),
            "segments": [s.to_dict() for s in self.segments],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobState:
        """Reconstruct a :class:`JobState` from :meth:`to_dict` output."""
        return cls(
            status=data.get("status", "created"),
            pid=data.get("pid"),
            phase=data.get("phase"),
            artifacts_done=int(data.get("artifacts_done", 0)),
            artifacts_total=int(data.get("artifacts_total", 0)),
            artifacts_cached=int(data.get("artifacts_cached", 0)),
            estimate=data.get("estimate"),
            cost_usd_total=float(data.get("cost_usd_total", 0.0)),
            cost_usd_segment=float(data.get("cost_usd_segment", 0.0)),
            usage_total=dict(data.get("usage_total", {})),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            started_at=data.get("started_at"),
            updated_at=data.get("updated_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
        )


def is_alive(pid: int | None) -> bool:
    """Best-effort liveness check for a job's recorded pid (POSIX signal 0)."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
