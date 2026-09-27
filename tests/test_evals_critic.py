"""The eval critic (``enterprise_sim.evals.critic``) and ``evals critique``/``verify``."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.fixtures import golden_run
from enterprise_sim.benchmark.generate import load_world_kg
from enterprise_sim.benchmark.schema import QAPair
from enterprise_sim.cli import main
from enterprise_sim.core.world import Node, World
from enterprise_sim.evals.critic import critique_pairs, structural_critique
from enterprise_sim.evals.store import read_questions

_T0 = datetime(2026, 1, 5, 9, 0)


def _world() -> World:
    world = World()
    for node in (
        Node(id="person:ana", type="Person", created_at=_T0, props={"name": "Ana Ruiz"}),
        Node(id="person:bo", type="Person", created_at=_T0, props={"name": "Bo Lee"}),
        Node(id="team:core", type="Team", created_at=_T0, props={"name": "Core"}),
        Node(
            id="project:billing",
            type="Project",
            created_at=_T0,
            props={"name": "Billing migration"},
        ),
        Node(
            id="init:billing",
            type="Initiative",
            created_at=_T0,
            props={"name": "Billing migration"},
        ),
    ):
        world.add_node(node)
    return world


def _pair(question: str, expected: tuple[str, ...], **kw: Any) -> QAPair:
    return QAPair(
        id=kw.pop("id", "qa-1"),
        question=question,
        qtype=kw.pop("qtype", "who"),
        reasoning_type=kw.pop("reasoning_type", "direct_relation"),
        expected_ids=expected,
        **kw,
    )


def test_a_clean_pair_keeps_full_confidence() -> None:
    result = structural_critique(_pair("Who does Ana Ruiz report to?", ("person:bo",)), _world())
    assert result.confidence == 1.0
    assert result.critique == "no issues found"


def test_an_ambiguous_subject_name_halves_confidence() -> None:
    pair = _pair("What artifacts mention Billing migration?", ("person:ana",), qtype="which")
    result = structural_critique(pair, _world())
    assert "names 2 different nodes (Initiative, Project)" in result.critique
    assert result.confidence <= 0.5


def test_a_who_question_answered_by_a_team_is_suspect() -> None:
    result = structural_critique(_pair("Who leads Core?", ("team:core",)), _world())
    assert "asks for Person but the answer includes Team" in result.critique
    assert result.confidence == pytest.approx(0.6)


def test_missing_or_empty_answers_get_zero_confidence() -> None:
    assert structural_critique(_pair("Who is Ana Ruiz?", ()), _world()).confidence == 0.0
    assert (
        structural_critique(_pair("Who is Ana Ruiz?", ("person:ghost",)), _world()).confidence
        == 0.0
    )


def test_a_count_label_that_disagrees_with_the_set_is_suspect() -> None:
    pair = _pair(
        "How many people are on Core?",
        ("person:ana", "person:bo"),
        qtype="count",
        reasoning_type="aggregation",
        expected_label="3",
    )
    result = structural_critique(pair, _world())
    assert "count label says 3" in result.critique


def test_duplicate_questions_with_different_answers_are_flagged() -> None:
    a = _pair("Who does Ana Ruiz report to?", ("person:bo",), id="qa-a")
    b = _pair("Who does Ana Ruiz report to?", ("person:ana",), id="qa-b")
    reviewed = critique_pairs([a, b], _world())
    assert all("appears 2 times with different answers" in (p.critique or "") for p in reviewed)


class _StubClient:
    """Stands in for an LLMClient: returns canned reviews, records the prompts."""

    def __init__(self, reviews: list[dict[str, Any]]) -> None:
        self.reviews = reviews
        self.prompts: list[str] = []

    def generate_structured(self, prompt: Any, schema: Any, **_: Any) -> Any:
        self.prompts.append(prompt.user_text)

        class _Result:
            data = {"reviews": self.reviews}

        return _Result()


def test_the_llm_review_combines_with_the_structural_one() -> None:
    ok = _pair("Who does Ana Ruiz report to?", ("person:bo",), id="qa-ok")
    bad = _pair("Who does Bo Lee report to?", ("person:ana",), id="qa-bad")
    client = _StubClient(
        [
            {"id": "qa-ok", "verdict": "ok", "confidence": 0.9, "reason": "matches the graph"},
            {"id": "qa-bad", "verdict": "wrong", "confidence": 0.8, "reason": "Bo is the manager"},
            {"id": "qa-unknown", "verdict": "ok", "confidence": 1.0, "reason": "ignored"},
        ]
    )
    reviewed = {p.id: p for p in critique_pairs([ok, bad], _world(), client=client)}  # type: ignore[arg-type]
    assert reviewed["qa-ok"].confidence == pytest.approx(0.9)
    assert reviewed["qa-ok"].critic == "llm"
    assert "model review (ok): matches the graph" in (reviewed["qa-ok"].critique or "")
    assert reviewed["qa-bad"].confidence == pytest.approx(0.2)  # a wrong verdict caps it
    # The prompt showed the model the answer and the subject's graph facts.
    assert "Who does Ana Ruiz report to?" in client.prompts[0]
    assert "Bo Lee [Person]" in client.prompts[0]


def test_human_verified_pairs_are_never_re_critiqued() -> None:
    pair = _pair(
        "Who leads Core?",
        ("team:core",),
        verified=True,
        confidence=1.0,
        critic="human",
        critique="verified by a human",
    )
    [reviewed] = critique_pairs([pair], _world())
    assert reviewed == pair


def test_review_fields_round_trip_and_are_omitted_when_unset() -> None:
    plain = _pair("Who is Ana Ruiz?", ("person:ana",))
    assert "confidence" not in plain.to_dict() and "verified" not in plain.to_dict()
    reviewed = replace(plain, confidence=0.4, critique="x", critic="structural", verified=True)
    assert QAPair.from_jsonl(reviewed.to_jsonl()) == reviewed


# --- CLI: critique / verify / regenerate keeps human work ----------------------


@pytest.fixture
def world_dir(tmp_path: Path, capsys: Any) -> Path:
    world_dir = golden_run(tmp_path).world_dir
    assert main(["evals", "generate", "--world", str(world_dir)]) == 0
    capsys.readouterr()
    return world_dir


def test_verify_corrects_an_answer_and_marks_it_human_verified(
    world_dir: Path, capsys: Any
) -> None:
    pair = next(p for p in read_questions(world_dir) if p.reasoning_type == "direct_relation")
    other = next(n.id for n in load_world_kg(world_dir).nodes() if n.id not in pair.expected_ids)
    assert (
        main(
            ["evals", "verify", "--world", str(world_dir), "--id", pair.id, "--expected-ids", other]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)["question"]
    assert out["id"] == pair.id  # the id is stable across a correction
    assert out["expected_ids"] == [other]
    assert out["verified"] is True
    assert out["confidence"] == 1.0
    assert out["critic"] == "human"
    assert out["critique"].startswith("corrected by a human")

    # Re-critiquing and regenerating both leave the human's answer alone.
    assert main(["evals", "critique", "--world", str(world_dir)]) == 0
    assert main(["evals", "generate", "--world", str(world_dir), "--force"]) == 0
    capsys.readouterr()
    kept = next(p for p in read_questions(world_dir) if p.id == pair.id)
    assert kept.expected_ids == (other,)
    assert kept.verified and kept.confidence == 1.0


def test_verify_without_changes_confirms_the_answer(world_dir: Path, capsys: Any) -> None:
    pair = next(iter(read_questions(world_dir)))
    assert main(["evals", "verify", "--world", str(world_dir), "--id", pair.id]) == 0
    out = json.loads(capsys.readouterr().out)["question"]
    assert out["expected_ids"] == list(pair.expected_ids)
    assert out["critique"] == "verified by a human"


def test_verify_rejects_unknown_nodes_and_questions(world_dir: Path, capsys: Any) -> None:
    pair = next(iter(read_questions(world_dir)))
    args = ["evals", "verify", "--world", str(world_dir), "--id", pair.id, "--expected-ids", "nope"]
    assert main(args) == 2
    assert main(["evals", "verify", "--world", str(world_dir), "--id", "qa-missing"]) == 2


def test_regenerate_keeps_proposed_questions(world_dir: Path, capsys: Any, tmp_path: Path) -> None:
    node = next(iter(load_world_kg(world_dir).nodes()))
    proposal = tmp_path / "p.json"
    proposal.write_text(
        json.dumps(
            [
                {
                    "question": "A hand-written question?",
                    "reasoning_type": "direct_relation",
                    "expected_ids": [node.id],
                }
            ]
        )
    )
    assert main(["evals", "add", "--world", str(world_dir), "--file", str(proposal)]) == 0
    assert main(["evals", "generate", "--world", str(world_dir), "--force"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["kept"] == 1
    assert any(p.source == "proposed" for p in read_questions(world_dir))
