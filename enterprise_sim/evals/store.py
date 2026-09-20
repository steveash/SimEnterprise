"""Per-run eval question-set storage: ``<run>/evals/questions.jsonl`` (EXPLORER_EVALS.md §1).

A run's eval question set is a :class:`~enterprise_sim.benchmark.schema.Benchmark`
persisted at a fixed path under the run directory. This module owns that
on-disk layout — read/write/append/remove — layered directly on
:class:`~enterprise_sim.benchmark.schema.Benchmark`'s existing JSONL codec, plus
the duplicate-id rejection ``evals add``/proposal-appending needs.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from enterprise_sim.benchmark.schema import Benchmark, QAPair

#: The file name under ``<run>/evals/``.
QUESTIONS_FILENAME = "questions.jsonl"


class DuplicateQuestionError(ValueError):
    """Raised when a :class:`QAPair` id already exists in the run's question set."""


def evals_dir(run_dir: str | Path) -> Path:
    """``<run>/evals/``."""
    return Path(run_dir) / "evals"


def questions_path(run_dir: str | Path) -> Path:
    """``<run>/evals/questions.jsonl``."""
    return evals_dir(run_dir) / QUESTIONS_FILENAME


def read_questions(run_dir: str | Path) -> Benchmark:
    """Read the run's question set; an empty :class:`Benchmark` if none exists yet."""
    path = questions_path(run_dir)
    if not path.is_file():
        return Benchmark()
    return Benchmark.read_jsonl(path)


def write_questions(run_dir: str | Path, benchmark: Benchmark) -> None:
    """Overwrite ``<run>/evals/questions.jsonl`` with ``benchmark`` (creates ``evals/``)."""
    path = questions_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    benchmark.write_jsonl(path)


def append_questions(run_dir: str | Path, pairs: Iterable[QAPair]) -> Benchmark:
    """Append ``pairs`` to the run's question set; reject any duplicate id.

    Validates every incoming pair's id is unique — both against the existing
    set and against the rest of the batch — *before* writing anything, so one
    bad pair in a batch never leaves a partially-appended file. Raises
    :class:`DuplicateQuestionError` naming the first duplicate found.
    """
    existing = read_questions(run_dir)
    existing_ids = {p.id for p in existing}
    incoming = list(pairs)
    seen_in_batch: set[str] = set()
    for pair in incoming:
        if pair.id in existing_ids:
            raise DuplicateQuestionError(f"question id {pair.id!r} already exists in {run_dir}")
        if pair.id in seen_in_batch:
            raise DuplicateQuestionError(f"question id {pair.id!r} appears twice in this batch")
        seen_in_batch.add(pair.id)
    merged = Benchmark.of([*existing, *incoming])
    write_questions(run_dir, merged)
    return merged


def remove_questions(
    run_dir: str | Path, ids: Iterable[str], *, only_source: str | None = "proposed"
) -> tuple[Benchmark, list[str]]:
    """Remove ``ids`` from the run's question set; returns ``(new set, ids removed)``.

    When ``only_source`` is set (default ``"proposed"``, per EXPLORER_EVALS.md
    §2 — "only proposed questions may be removed"), an id whose pair has a
    different ``source`` is left in place and simply absent from the returned
    removed-ids list rather than raising: ``generated`` pairs are the
    deterministic derivation from the gold KG, and removing one would desync
    the set from ``bench generate``/``evals generate``.
    """
    existing = read_questions(run_dir)
    wanted = set(ids)
    keep: list[QAPair] = []
    removed: list[str] = []
    for pair in existing:
        if pair.id in wanted and (only_source is None or pair.source == only_source):
            removed.append(pair.id)
            continue
        keep.append(pair)
    result = Benchmark.of(keep)
    if removed:
        write_questions(run_dir, result)
    return result, removed


__all__ = [
    "QUESTIONS_FILENAME",
    "DuplicateQuestionError",
    "append_questions",
    "evals_dir",
    "questions_path",
    "read_questions",
    "remove_questions",
    "write_questions",
]
