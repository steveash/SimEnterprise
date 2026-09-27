"""Directory layout, the ``.world`` index, gold-KG export, consistency validator.

The assembly (Layer D) endpoints: :func:`execute_run` builds a world directory
end to end and records it in its ``.world`` (:mod:`enterprise_sim.worlds`);
:func:`estimate_run` prices a build without writing anything.
"""

from __future__ import annotations

from enterprise_sim.assembly.corpus import CorpusResult, RenderEstimate, build_corpus
from enterprise_sim.assembly.runner import (
    WORLD_OUTPUTS,
    RunResult,
    build_world_file,
    canonical_config,
    compute_config_digest,
    compute_world_id,
    effective_backend,
    estimate_run,
    execute_run,
    llm_config_for,
)
from enterprise_sim.assembly.validation import (
    summarize_issue_rows,
    validate_consistency,
)

__all__ = [
    "WORLD_OUTPUTS",
    "CorpusResult",
    "RenderEstimate",
    "RunResult",
    "build_corpus",
    "build_world_file",
    "canonical_config",
    "compute_config_digest",
    "compute_world_id",
    "effective_backend",
    "estimate_run",
    "execute_run",
    "llm_config_for",
    "summarize_issue_rows",
    "validate_consistency",
]
