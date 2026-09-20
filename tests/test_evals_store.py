"""Tests for ``enterprise_sim.evals.store``: the per-run ``evals/questions.jsonl`` layout."""

from __future__ import annotations

from pathlib import Path

import pytest
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.evals.store import (
    DuplicateQuestionError,
    append_questions,
    evals_dir,
    questions_path,
    read_questions,
    remove_questions,
    write_questions,
)


def _pair(
    id_: str, *, source: str = "generated", reasoning_type: str = "direct_relation"
) -> QAPair:
    return QAPair(
        id=id_,
        question=f"Question {id_}?",
        qtype="who",
        reasoning_type=reasoning_type,
        expected_ids=("node:1",),
        source=source,
    )


def test_paths() -> None:
    assert evals_dir("run") == Path("run") / "evals"
    assert questions_path("run") == Path("run") / "evals" / "questions.jsonl"


def test_read_questions_empty_when_absent(tmp_path: Path) -> None:
    benchmark = read_questions(tmp_path)
    assert len(benchmark) == 0


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    benchmark = Benchmark.of([_pair("qa-1"), _pair("qa-2")])
    write_questions(tmp_path, benchmark)

    loaded = read_questions(tmp_path)

    assert [p.id for p in loaded] == ["qa-1", "qa-2"]
    assert questions_path(tmp_path).is_file()


def test_append_questions_merges(tmp_path: Path) -> None:
    write_questions(tmp_path, Benchmark.of([_pair("qa-1")]))

    merged = append_questions(tmp_path, [_pair("qa-2"), _pair("qa-3")])

    assert [p.id for p in merged] == ["qa-1", "qa-2", "qa-3"]
    assert [p.id for p in read_questions(tmp_path)] == ["qa-1", "qa-2", "qa-3"]


def test_append_questions_rejects_duplicate_against_existing(tmp_path: Path) -> None:
    write_questions(tmp_path, Benchmark.of([_pair("qa-1")]))

    with pytest.raises(DuplicateQuestionError):
        append_questions(tmp_path, [_pair("qa-1")])

    # nothing was written on the failed call.
    assert [p.id for p in read_questions(tmp_path)] == ["qa-1"]


def test_append_questions_rejects_duplicate_within_batch(tmp_path: Path) -> None:
    with pytest.raises(DuplicateQuestionError):
        append_questions(tmp_path, [_pair("qa-1"), _pair("qa-1")])

    assert len(read_questions(tmp_path)) == 0


def test_remove_questions_only_removes_proposed_by_default(tmp_path: Path) -> None:
    write_questions(
        tmp_path,
        Benchmark.of([_pair("qa-1", source="generated"), _pair("qa-2", source="proposed")]),
    )

    result, removed = remove_questions(tmp_path, ["qa-1", "qa-2"])

    assert removed == ["qa-2"]
    assert [p.id for p in result] == ["qa-1"]
    assert [p.id for p in read_questions(tmp_path)] == ["qa-1"]


def test_remove_questions_no_op_when_nothing_matches(tmp_path: Path) -> None:
    write_questions(tmp_path, Benchmark.of([_pair("qa-1", source="generated")]))

    result, removed = remove_questions(tmp_path, ["qa-1"])

    assert removed == []
    assert [p.id for p in result] == ["qa-1"]


def test_remove_questions_only_source_none_removes_anything(tmp_path: Path) -> None:
    write_questions(tmp_path, Benchmark.of([_pair("qa-1", source="generated")]))

    result, removed = remove_questions(tmp_path, ["qa-1"], only_source=None)

    assert removed == ["qa-1"]
    assert len(result) == 0
