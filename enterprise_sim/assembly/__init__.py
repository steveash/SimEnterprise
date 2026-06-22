"""Directory layout, manifest, gold-KG export, consistency validator.

M1 implements the assembly (Layer D) endpoints used by an empty reproducible
run: the :class:`Manifest` index and the :func:`execute_run` skeleton that lays
out the run directory and snapshots the config.
"""

from __future__ import annotations

from enterprise_sim.assembly.manifest import (
    SCHEMA_VERSION,
    VOLATILE_FIELDS,
    Manifest,
    structural_view,
)
from enterprise_sim.assembly.runner import (
    RunResult,
    build_manifest,
    compute_config_digest,
    compute_run_id,
    execute_run,
)

__all__ = [
    "SCHEMA_VERSION",
    "VOLATILE_FIELDS",
    "Manifest",
    "RunResult",
    "build_manifest",
    "compute_config_digest",
    "compute_run_id",
    "execute_run",
    "structural_view",
]
