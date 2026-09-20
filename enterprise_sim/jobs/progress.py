"""Progress events the run pipeline reports into (``docs/EXPLORER_RUNS.md`` §3.1).

A thread-safe sink the pipeline reports into as it works: one JSON object per
event, ``kind``-tagged, with a wall-clock stamp and a kind-specific payload. The
same objects are meant to go to ``stdout`` (for a supervising sidecar) *and* to
``<job>/progress.jsonl`` (so a UI can replay the timeline after a restart) —
:class:`JsonlSink` does both. This module has no dependency on the rest of the
simulator, so :mod:`enterprise_sim.assembly` can depend on it for the pipeline's
progress hooks without any import cycle back into :mod:`enterprise_sim.jobs`.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO, runtime_checkable

__all__ = ["JsonlSink", "NullSink", "ProgressEvent", "ProgressSink"]


@runtime_checkable
class ProgressSink(Protocol):
    """Anything that can receive a stream of :class:`ProgressEvent`\\ s."""

    def emit(self, event: ProgressEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One reported step of a run (``docs/EXPLORER_RUNS.md`` §3.1's event table).

    ``kind`` is one of ``phase`` / ``world_built`` / ``scheduled`` / ``estimate``
    / ``artifact`` / ``paused`` / ``error`` / ``done`` (plus ``warning`` for a
    non-fatal job-level notice, e.g. a failed eval-generation finalize step).
    ``data`` is the kind-specific payload; always JSON-serializable.
    """

    kind: str
    ts: float
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping this event serializes to."""
        return {"kind": self.kind, "ts": self.ts, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProgressEvent:
        """Reconstruct a :class:`ProgressEvent` from :meth:`to_dict` output."""
        return cls(kind=data["kind"], ts=float(data["ts"]), data=dict(data.get("data", {})))

    @classmethod
    def now(cls, kind: str, **data: Any) -> ProgressEvent:
        """Build an event stamped with the current wall clock (the common case)."""
        return cls(kind=kind, ts=time.time(), data=data)


class NullSink:
    """A :class:`ProgressSink` that discards every event.

    The default when no sink is given — every pipeline entry point that takes an
    optional ``progress`` parameter treats ``None`` as "use this", so an
    unmonitored run costs nothing extra and stays byte-identical to before these
    hooks existed.
    """

    def emit(self, event: ProgressEvent) -> None:
        """Discard ``event``."""
        return None


class JsonlSink:
    """Writes one JSON line per event to ``stream``, and appends to a file too.

    Thread-safe: the render phase fans out across a bounded thread pool
    (``LLMClient.generate_many``), and each scenario's renderer emits its own
    ``artifact`` events concurrently — a lock around both writes keeps lines from
    interleaving. ``also_to`` (typically ``<job>/progress.jsonl``) is opened once
    in append mode and flushed after every line so a reattached UI (or a killed
    process's next ``job status``) sees a consistent, un-truncated file.
    """

    def __init__(self, stream: TextIO, *, also_to: Path | None = None) -> None:
        self._stream = stream
        self._file: TextIO | None = (
            also_to.open("a", encoding="utf-8") if also_to is not None else None
        )
        self._lock = threading.Lock()

    def emit(self, event: ProgressEvent) -> None:
        """Write ``event`` as one JSON line to the stream and the file (if any)."""
        line = json.dumps(event.to_dict(), sort_keys=True)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()

    def close(self) -> None:
        """Close the ``also_to`` file handle, if one was opened."""
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
