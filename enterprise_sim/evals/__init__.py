"""Per-run KG-QA eval question sets: storage, proposals, sampling, runners (EXPLORER_EVALS.md).

Thin, JSON-speaking wrappers over :mod:`enterprise_sim.benchmark` aware of the
per-run ``<run>/evals/questions.jsonl`` layout
(:mod:`enterprise_sim.evals.store`): validating and appending AI-proposed
questions (:mod:`enterprise_sim.evals.proposals`), deterministic sampling
(:mod:`enterprise_sim.evals.sample`, the reference a TypeScript port matches
bit-for-bit), and streaming a benchmark runner over a selection
(:mod:`enterprise_sim.evals.runner`). :mod:`enterprise_sim.evals.cli` wires the
``enterprise-sim evals`` subcommand group.
"""

from __future__ import annotations

from enterprise_sim.evals.proposals import Rejection, load_node_ids, qid, validate_proposals
from enterprise_sim.evals.runner import (
    RUNNERS,
    RunnerUnavailable,
    check_prereqs,
    report_to_dict,
    run_eval,
)
from enterprise_sim.evals.sample import Mulberry32, sample, sample_ids, seeded_shuffle
from enterprise_sim.evals.store import (
    QUESTIONS_FILENAME,
    DuplicateQuestionError,
    append_questions,
    evals_dir,
    questions_path,
    read_questions,
    remove_questions,
    write_questions,
)

__all__ = [
    "QUESTIONS_FILENAME",
    "RUNNERS",
    "DuplicateQuestionError",
    "Mulberry32",
    "Rejection",
    "RunnerUnavailable",
    "append_questions",
    "check_prereqs",
    "evals_dir",
    "load_node_ids",
    "qid",
    "questions_path",
    "read_questions",
    "remove_questions",
    "report_to_dict",
    "run_eval",
    "sample",
    "sample_ids",
    "seeded_shuffle",
    "validate_proposals",
    "write_questions",
]
