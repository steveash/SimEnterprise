"""Question answerability: evaluate business questions against the sampled data.

The question loop's measurement half. Each :class:`QuestionSpec` carries the
SQL that *should* answer it; a question is **answerable** when that SQL
executes against the scenario's tables + materialized views, returns at least
one row, and produces at least one non-null value. Anything else — missing
column, missing table, empty result, all-null aggregate — is a **gap**, and
the question's :class:`GapFix` (template mode) or an LLM proposal (llm mode)
says how the data must change to close it.

This module is pure evaluation + bookkeeping; the iterate/patch/resample loop
lives in ``runner.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from enterprise_sim.data_products.spec import QuestionSpec, ScenarioSpec, SpecDelta
from enterprise_sim.data_products.views import connect_scenario

__all__ = [
    "QuestionAttempt",
    "QuestionState",
    "collect_gap_deltas",
    "evaluate_questions",
    "questions_payload",
]

# Cap on rows fetched per evaluation: enough to prove non-emptiness and sample
# the shape without dragging a huge result set through memory.
_FETCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class QuestionAttempt:
    """One evaluation of one question in one loop iteration."""

    iteration: int
    ok: bool
    rows: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"iteration": self.iteration, "ok": self.ok, "rows": self.rows}
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class QuestionState:
    """A question's accumulated history across loop iterations."""

    question: QuestionSpec
    attempts: list[QuestionAttempt] = field(default_factory=list)

    @property
    def answerable(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].ok

    @property
    def first_answerable_iteration(self) -> int | None:
        for attempt in self.attempts:
            if attempt.ok:
                return attempt.iteration
        return None


def _evaluate_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> tuple[bool, int, str | None]:
    """Run one question's SQL: (ok, row_count_capped, error)."""
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(_FETCH_LIMIT)
    except duckdb.Error as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"
    if not rows:
        return False, 0, "query returned no rows"
    has_value = any(value is not None for row in rows for value in row)
    if not has_value:
        return False, len(rows), "query returned only NULLs"
    return True, len(rows), None


def evaluate_questions(
    spec: ScenarioSpec,
    data_dir: Path,
    states: list[QuestionState],
    *,
    iteration: int,
    only_unanswered: bool = True,
) -> list[QuestionState]:
    """Evaluate questions against the current data, appending to each state.

    ``only_unanswered`` skips re-running questions that already passed in an
    earlier iteration (their data only ever grows, and re-proving them each
    round doubles the loop's cost for no signal).
    """
    conn = connect_scenario(spec, data_dir, include_views=True)
    try:
        for state in states:
            if only_unanswered and state.answerable:
                continue
            ok, rows, error = _evaluate_sql(conn, state.question.sql)
            state.attempts.append(
                QuestionAttempt(iteration=iteration, ok=ok, rows=rows, error=error)
            )
    finally:
        conn.close()
    return states


def collect_gap_deltas(states: list[QuestionState]) -> tuple[SpecDelta, ...]:
    """The deduplicated spec deltas proposed by failing questions' gap fixes.

    Deduplication is structural (identical delta payloads collapse), so two
    questions gated on the same missing column produce one change.
    """
    seen: set[str] = set()
    deltas: list[SpecDelta] = []
    for state in states:
        if state.answerable or state.question.gap is None:
            continue
        for delta in state.question.gap.deltas:
            key = delta.model_dump_json()
            if key not in seen:
                seen.add(key)
                deltas.append(delta)
    return tuple(deltas)


def questions_payload(
    spec: ScenarioSpec, states: list[QuestionState], *, iterations_run: int
) -> dict[str, Any]:
    """The final ``questions.json`` document for a data-product run."""
    answerable = sum(1 for state in states if state.answerable)
    return {
        "scenario": spec.name,
        "title": spec.title,
        "iterations_run": iterations_run,
        "categories": list(spec.question_categories),
        "total_questions": len(states),
        "answerable_questions": answerable,
        "questions": [
            {
                "id": state.question.id,
                "category": state.question.category,
                "question": state.question.question,
                "sql": state.question.sql,
                "answerable": state.answerable,
                "first_answerable_iteration": state.first_answerable_iteration,
                "gap": (
                    {
                        "description": state.question.gap.description,
                        "deltas": [
                            delta.model_dump(mode="json", exclude_none=True)
                            for delta in state.question.gap.deltas
                        ],
                    }
                    if state.question.gap is not None
                    else None
                ),
                "attempts": [attempt.to_dict() for attempt in state.attempts],
            }
            for state in states
        ],
    }
