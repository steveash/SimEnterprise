"""Tests for ``enterprise_sim.evals.proposals``: validating AI-proposed questions."""

from __future__ import annotations

import json
from pathlib import Path

from enterprise_sim.evals.proposals import qid, validate_proposals
from enterprise_sim.evals.store import append_questions, read_questions


def _run_with_nodes(tmp_path: Path, node_ids: list[str]) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "kg").mkdir(parents=True)
    with (run_dir / "kg" / "nodes.jsonl").open("w", encoding="utf-8") as f:
        for node_id in node_ids:
            f.write(json.dumps({"id": node_id, "type": "Person"}) + "\n")
    return run_dir


def test_valid_proposal_is_accepted() -> None:
    ids = frozenset({"person:a"})
    accepted, rejected = validate_proposals(
        "unused",
        [
            {
                "question": "Who is A?",
                "reasoning_type": "direct_relation",
                "expected_ids": ["person:a"],
            }
        ],
        node_ids=ids,
    )
    assert rejected == []
    assert len(accepted) == 1
    pair = accepted[0]
    assert pair.source == "proposed"
    assert pair.expected_ids == ("person:a",)
    assert pair.id == qid("direct_relation", "what", "Who is A?", ("person:a",))


def test_rejects_missing_question() -> None:
    accepted, rejected = validate_proposals(
        "unused",
        [{"reasoning_type": "direct_relation", "expected_ids": ["person:a"]}],
        node_ids=frozenset({"person:a"}),
    )
    assert accepted == []
    assert len(rejected) == 1
    assert "empty question" in rejected[0].reason


def test_rejects_bad_reasoning_type() -> None:
    accepted, rejected = validate_proposals(
        "unused",
        [{"question": "Q?", "reasoning_type": "bogus", "expected_ids": ["person:a"]}],
        node_ids=frozenset({"person:a"}),
    )
    assert accepted == []
    assert "bogus" in rejected[0].reason


def test_rejects_empty_expected_ids() -> None:
    accepted, rejected = validate_proposals(
        "unused",
        [{"question": "Q?", "reasoning_type": "direct_relation", "expected_ids": []}],
        node_ids=frozenset({"person:a"}),
    )
    assert accepted == []
    assert "expected_ids is empty" in rejected[0].reason


def test_rejects_unknown_expected_id() -> None:
    accepted, rejected = validate_proposals(
        "unused",
        [{"question": "Q?", "reasoning_type": "direct_relation", "expected_ids": ["person:ghost"]}],
        node_ids=frozenset({"person:a"}),
    )
    assert accepted == []
    assert "person:ghost" in rejected[0].reason


def test_rejects_duplicate_within_batch() -> None:
    proposal = {"question": "Q?", "reasoning_type": "direct_relation", "expected_ids": ["person:a"]}
    accepted, rejected = validate_proposals(
        "unused", [proposal, dict(proposal)], node_ids=frozenset({"person:a"})
    )
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert "duplicate" in rejected[0].reason


def test_rejects_duplicate_against_existing_run_questions(tmp_path: Path) -> None:
    run_dir = _run_with_nodes(tmp_path, ["person:a"])
    proposal = {"question": "Q?", "reasoning_type": "direct_relation", "expected_ids": ["person:a"]}
    accepted, _ = validate_proposals(run_dir, [proposal])
    append_questions(run_dir, accepted)

    accepted2, rejected2 = validate_proposals(run_dir, [proposal])

    assert accepted2 == []
    assert "duplicate" in rejected2[0].reason


def test_load_node_ids_reads_kg_nodes_jsonl(tmp_path: Path) -> None:
    run_dir = _run_with_nodes(tmp_path, ["a", "b", "c"])
    from enterprise_sim.evals.proposals import load_node_ids

    assert load_node_ids(run_dir) == frozenset({"a", "b", "c"})


def test_mixed_batch_partial_accept() -> None:
    proposals = [
        {"question": "Good?", "reasoning_type": "direct_relation", "expected_ids": ["person:a"]},
        {"question": "Bad reasoning", "reasoning_type": "nope", "expected_ids": ["person:a"]},
        {"question": "Bad id", "reasoning_type": "direct_relation", "expected_ids": ["ghost"]},
    ]
    accepted, rejected = validate_proposals("unused", proposals, node_ids=frozenset({"person:a"}))
    assert len(accepted) == 1
    assert accepted[0].question == "Good?"
    assert len(rejected) == 2


def test_accepted_proposal_round_trips_through_the_run_store(tmp_path: Path) -> None:
    run_dir = _run_with_nodes(tmp_path, ["person:a"])
    proposal = {
        "question": "Who is A?",
        "reasoning_type": "direct_relation",
        "expected_ids": ["person:a"],
        "tags": ["demo"],
    }
    accepted, rejected = validate_proposals(run_dir, [proposal])
    assert rejected == []
    append_questions(run_dir, accepted)

    stored = list(read_questions(run_dir))
    assert len(stored) == 1
    assert stored[0].source == "proposed"
    assert stored[0].tags == ("demo",)
