"""One deterministic gold KG for generators and tests.

The whole benchmark is generated from — and scored against — the *golden run*:
the committed ``examples/golden.toml`` config executed end to end, exactly as
``tests/test_golden_run.py`` does. Per the epic (esim-uzc), the benchmark never
depends on a checked-in run directory; it executes the run fresh so the gold
graph and the answer key always agree.

This module is the single source of that ground truth. :func:`load_gold_world`
runs the golden config into a temporary directory (via the deterministic, no-
network ``fake`` backend) and returns the in-memory
:class:`~enterprise_sim.core.world.World` — the gold knowledge graph — so every
generator and test shares one byte-stable source. :func:`golden_run` returns the
full :class:`~enterprise_sim.assembly.RunResult` when a caller also needs the
corpus or the run directory on disk (e.g. the RAG baseline, esim-uzc.5).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from enterprise_sim.assembly import RunResult, execute_run
from enterprise_sim.core.config import RunConfig, load_config
from enterprise_sim.core.world import World

# The committed golden config — the documented v1 vertical slice (PLAN.md §4).
# Resolved relative to the repo root (this file is enterprise_sim/benchmark/…).
GOLDEN_CONFIG: Path = Path(__file__).resolve().parents[2] / "examples" / "golden.toml"


def golden_config(output_dir: str | Path) -> RunConfig:
    """Load the committed golden config, redirected to ``output_dir``.

    Mirrors ``tests/test_golden_run.py``: the on-disk config is loaded unchanged
    except for its ``output_dir``, so the run lands wherever the caller wants
    (typically a temporary directory).
    """
    config = load_config(GOLDEN_CONFIG)
    return config.model_copy(update={"output_dir": Path(output_dir)})


def golden_run(output_dir: str | Path) -> RunResult:
    """Execute the golden run into ``output_dir`` and return its :class:`RunResult`.

    The run uses the default deterministic ``fake`` LLM backend (no network, no
    cost) so the result is reproducible. The caller owns ``output_dir`` and its
    lifetime; use :func:`load_gold_world` when only the in-memory gold KG is
    needed and the run directory can be discarded.
    """
    return execute_run(golden_config(output_dir))


def load_gold_world() -> World:
    """Execute the golden run in a throwaway temp dir and return the gold KG.

    Returns the in-memory :class:`~enterprise_sim.core.world.World` produced by
    the run — the gold knowledge graph generators query and the grader scores
    against. The temporary run directory is removed before returning; nothing on
    disk is needed because the :class:`World` is fully in memory.
    """
    with tempfile.TemporaryDirectory(prefix="esim-bench-gold-") as tmp:
        return golden_run(tmp).world
