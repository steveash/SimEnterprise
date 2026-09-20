"""Run-job orchestration: progress, cooperative pause, ledger, worker, extend.

Implements the Python side of the run launcher (``docs/EXPLORER_RUNS.md`` §3):
launching a run as a resumable, cost-tracked *job* with live progress, and
extending a finished run with more time or more scenario instances. This
package is deliberately not imported eagerly from here (submodules are
imported directly, e.g. ``from enterprise_sim.jobs.worker import run_job``) —
``progress`` and ``control`` are leaf modules with no dependency on the rest of
the simulator, and ``enterprise_sim.assembly`` depends on *them* (for the
pipeline's progress/pause hooks), so keeping this ``__init__`` empty avoids any
import-order cycle between the two packages.
"""

from __future__ import annotations
