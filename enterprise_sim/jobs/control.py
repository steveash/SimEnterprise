"""Cooperative pause for a running job (``docs/EXPLORER_RUNS.md`` §3.2).

``RunControl`` is the one flag the render loop checks before starting each
deliverable event: a file-backed request (``control.json``, so a separate
process — the CLI's ``job pause`` — can ask a running worker to stop) or an
in-process signal flag (``SIGTERM``/``SIGINT``, so closing the app pauses
rather than corrupts). This module has no dependency on the rest of the
simulator, so :mod:`enterprise_sim.assembly` can depend on it for the
pipeline's pause hook without any import cycle back into :mod:`enterprise_sim.jobs`.
"""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

__all__ = ["RunControl", "RunPaused", "install_signal_handlers"]


class RunPaused(Exception):
    """Raised inside the render loop when a cooperative pause was requested."""


class RunControl:
    """Tracks whether a running job should pause (``docs/EXPLORER_RUNS.md`` §3.2).

    ``should_pause`` is cheap to call before every event: it only re-reads
    ``control.json`` when the file's mtime has changed since the last check, and
    otherwise returns a cached value (or ``True`` immediately once a signal was
    received, without touching the filesystem at all).
    """

    def __init__(self, job_dir: Path) -> None:
        self._path = Path(job_dir) / "control.json"
        self._signalled = threading.Event()
        self._mtime: float | None = None
        self._cached_pause = False

    def request_pause(self) -> None:
        """Write ``control.json {"pause": true}`` (atomic; idempotent)."""
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pause": True}) + "\n", encoding="utf-8")
        tmp.replace(self._path)

    def clear(self) -> None:
        """Remove any pending pause request, file and in-process flag alike.

        Called at the start of a run/resume so a stale ``control.json`` left over
        from a prior segment (or a signal caught during an earlier attempt) never
        pauses a fresh attempt before it starts.
        """
        self._signalled.clear()
        self._path.unlink(missing_ok=True)
        self._mtime = None
        self._cached_pause = False

    def signal_pause(self) -> None:
        """Set the in-process pause flag (what the ``SIGTERM``/``SIGINT`` handler calls)."""
        self._signalled.set()

    def should_pause(self) -> bool:
        """Whether a pause was requested, via the signal flag or ``control.json``."""
        if self._signalled.is_set():
            return True
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return False
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            self._cached_pause = bool(data.get("pause", False))
        return self._cached_pause


def install_signal_handlers(control: RunControl) -> Callable[[], None]:
    """Install ``SIGTERM``/``SIGINT`` handlers that request a graceful pause.

    Returns a zero-arg callable that restores whatever handlers were previously
    installed; callers must invoke it when the run ends (success, pause, or
    failure) so a worker's handler never leaks into unrelated code running later
    in the same process (notably the test suite). Silently does nothing for a
    signal that cannot be caught here — not the main thread, or an unsupported
    platform signal — so a worker embedded in another context still runs; only
    the file-backed pause is guaranteed in that case.
    """

    def _handler(signum: int, frame: FrameType | None) -> None:
        control.signal_pause()

    previous: dict[int, Any] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):
            continue

    def _restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return _restore
