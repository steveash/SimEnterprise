"""The live-call governor: one account-wide cap on concurrent provider calls.

A provider quota is shared by every process on an account, so the cap that
protects it cannot live in one client's thread pool (``max_concurrency`` bounds
one client only, and a build may run several clients and several builds may
run at once). :class:`CallGovernor` is the single choke point every *live*
backend attempt goes through (:meth:`LLMClient._call`):

* **Slots** — at most ``slots`` attempts are in flight across all processes
  that share the gate directory: an attempt holds an exclusive ``flock`` on one
  of ``slots`` slot files for its duration. Cache hits never take a slot.
* **Rate-limit monitor** — every attempt appends ``<epoch> <0|1>`` (1 = rate
  limited) to ``outcomes.log`` in the gate directory. When, over the last
  ``window_seconds``, at least ``min_samples`` attempts were made and more than
  ``max_rate_limited_share`` of them were rate limited, the governor writes a
  ``STOP`` file and raises :class:`RateLimitTripped`. Every process sharing the
  gate then refuses new attempts until a person removes ``STOP``.

The gate directory comes from :attr:`LLMConfig.gate_dir` or, when that is unset,
the ``SIMENTERPRISE_CALL_GATE`` environment variable (slots from
``SIMENTERPRISE_CALL_SLOTS``, default 2). Without either, live calls are bounded
by ``max_concurrency`` only, as before. Offline backends (``fake``, ``synth``)
never use a governor. None of this is part of a world's config digest: it
decides when calls happen, never what they say.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from enterprise_sim.core.llm.types import LLMError

__all__ = [
    "GATE_ENV",
    "SLOTS_ENV",
    "CallGovernor",
    "RateLimitTripped",
]

GATE_ENV = "SIMENTERPRISE_CALL_GATE"
SLOTS_ENV = "SIMENTERPRISE_CALL_SLOTS"
_TAIL_BYTES = 1 << 20  # ~60k outcome lines: far more than a 10-minute window holds


class RateLimitTripped(LLMError):
    """The shared rate-limit share crossed its threshold; the build must stop."""


class CallGovernor:
    """Cross-process slots and a rolling rate-limit monitor over one directory."""

    def __init__(
        self,
        directory: str | Path,
        *,
        slots: int = 2,
        window_seconds: float = 600.0,
        max_rate_limited_share: float = 0.10,
        min_samples: int = 20,
        clock: Callable[[], float] = time.time,
        poll_seconds: float = 0.25,
    ) -> None:
        if slots < 1:
            raise ValueError("a call governor needs at least one slot")
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._slots = slots
        self._window = window_seconds
        self._threshold = max_rate_limited_share
        self._min_samples = min_samples
        self._clock = clock
        self._poll = poll_seconds
        self._log_lock = threading.Lock()

    @classmethod
    def from_env(cls, gate_dir: str | None, slots: int | None) -> CallGovernor | None:
        """The governor a live client uses: config first, then the environment."""
        directory = gate_dir or os.environ.get(GATE_ENV)
        if not directory:
            return None
        count = slots if slots is not None else int(os.environ.get(SLOTS_ENV, "2"))
        return cls(directory, slots=count)

    @property
    def stop_file(self) -> Path:
        return self._dir / "STOP"

    @property
    def slots(self) -> int:
        return self._slots

    def check(self) -> None:
        """Raise :class:`RateLimitTripped` if this gate has been stopped."""
        if self.stop_file.exists():
            reason = self.stop_file.read_text(encoding="utf-8").strip()
            raise RateLimitTripped(f"call gate stopped: {reason}")

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one of the shared slots for the duration of one attempt."""
        self.check()
        while True:
            for index in range(self._slots):
                fd = os.open(self._dir / f"slot-{index}", os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                return
            time.sleep(self._poll)
            self.check()

    def record(self, *, rate_limited: bool) -> None:
        """Log one attempt's outcome; trip the gate if the window's share is too high."""
        now = self._clock()
        line = f"{now:.3f} {1 if rate_limited else 0}\n".encode()
        log = self._dir / "outcomes.log"
        with self._log_lock:
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        if not rate_limited:
            return
        total, limited = self.window_counts(now)
        if total >= self._min_samples and limited > self._threshold * total:
            reason = (
                f"{limited} of {total} attempts rate limited in the last "
                f"{self._window:.0f} s (threshold {self._threshold:.0%})"
            )
            self.stop_file.write_text(reason + "\n", encoding="utf-8")
            raise RateLimitTripped(f"call gate stopped: {reason}")

    def window_counts(self, now: float | None = None) -> tuple[int, int]:
        """``(attempts, rate_limited)`` over the rolling window, across processes."""
        now = self._clock() if now is None else now
        log = self._dir / "outcomes.log"
        if not log.exists():
            return 0, 0
        with log.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read()
        if size > _TAIL_BYTES:
            data = data.split(b"\n", 1)[-1]  # drop a partial first line
        total = limited = 0
        for raw in data.splitlines():
            stamp, _, flag = raw.partition(b" ")
            try:
                when = float(stamp)
            except ValueError:
                continue
            if now - when <= self._window:
                total += 1
                limited += flag == b"1"
        return total, limited
