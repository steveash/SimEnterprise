"""Job directory + ledger: ``state.json``, segments, job id (``docs/EXPLORER_RUNS.md`` §3.3).

``state.json`` is the single source of truth a UI lists jobs from: status,
progress counters, the running cost ledger, and the list of render *segments*
(one per ``job run`` attempt — a fresh process, a resume after a pause, or a
retry after a failure). Cache hits are priced at $0 (``CostTracker``), so a
job's total spend across segments has to be summed from this ledger rather than
read off any single segment's client.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "JobState",
    "Segment",
    "generate_job_id",
    "is_alive",
    "read_state",
    "state_path",
    "write_state",
]

_STATE_FILE = "state.json"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "job"


def generate_job_id(company_name: str, *, now: datetime | None = None) -> str:
    """A unique, sortable, human-readable job id: ``<yyyymmdd-hhmmss>-<slug>``."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_slugify(company_name)}"


@dataclass(slots=True)
class Segment:
    """One render attempt: a ``job run`` invocation from start to pause/done/failure."""

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
    """The contents of ``state.json`` (``docs/EXPLORER_RUNS.md`` §3.3's table).

    ``status`` is one of ``"created"`` / ``"running"`` / ``"paused"`` /
    ``"failed"`` / ``"done"`` / ``"cancelled"``. A job whose status is
    ``"running"`` but whose ``pid`` is dead (see :func:`is_alive`) is shown by a
    UI as *interrupted* (resumable) rather than actually running.
    """

    status: str = "created"
    pid: int | None = None
    run_id: str | None = None
    run_dir: str | None = None
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
            "run_id": self.run_id,
            "run_dir": self.run_dir,
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
            run_id=data.get("run_id"),
            run_dir=data.get("run_dir"),
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


def state_path(job_dir: Path) -> Path:
    """The path to ``job_dir``'s ``state.json``."""
    return Path(job_dir) / _STATE_FILE


def read_state(job_dir: Path) -> JobState | None:
    """Read ``job_dir``'s ``state.json``, or ``None`` if it does not exist yet."""
    path = state_path(job_dir)
    if not path.is_file():
        return None
    return JobState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_state(job_dir: Path, state: JobState) -> None:
    """Atomically write ``state.json`` (tmp + replace), stamping ``updated_at``."""
    state.updated_at = datetime.now(UTC).isoformat()
    path = state_path(job_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def is_alive(pid: int | None) -> bool:
    """Best-effort liveness check for a job's recorded pid (POSIX signal 0)."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
