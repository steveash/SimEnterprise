"""``enterprise-sim evals`` CLI tests: parity with ``bench``, plus the full CLI surface.

Keyless and deterministic throughout (the golden run's ``fake`` backend);
``evals run`` (the only path that needs a real model) is exercised only for
its clean-error path without a key — mirroring
``tests/test_benchmark_keyless.py``'s ``requires_llm_runner`` gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.fixtures import golden_run
from enterprise_sim.benchmark.schema import Benchmark
from enterprise_sim.benchmark.score import Predictions, score
from enterprise_sim.cli import main


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("evals-cli-golden")
    result = golden_run(out)
    return result.world_dir


# -- generate matches bench generate, byte for byte ----------------------------


def test_evals_generate_matches_bench_generate_plus_a_review(
    world_dir: Path, capsys: Any, tmp_path: Path
) -> None:
    bench_out = tmp_path / "bench.jsonl"
    assert main(["bench", "generate", "--world", str(world_dir), "-o", str(bench_out)]) == 0
    capsys.readouterr()

    assert main(["evals", "generate", "--world", str(world_dir), "--force"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert sum(payload["confidence"].values()) == payload["count"]

    review = {"confidence", "critique", "critic", "verified"}
    evals_rows = [
        json.loads(line)
        for line in (world_dir / "evals" / "questions.jsonl").read_text().splitlines()
    ]
    bench_rows = [json.loads(line) for line in bench_out.read_text().splitlines()]
    # The same questions, in the same order, with the same answers — the evals
    # set just carries the critic's review on top.
    assert [{k: v for k, v in r.items() if k not in review} for r in evals_rows] == bench_rows
    for row in evals_rows:
        assert 0.0 <= row["confidence"] <= 1.0
        assert row["critic"] == "structural"
        assert row["critique"]


def test_evals_generate_refuses_to_overwrite_without_force(tmp_path: Path, capsys: Any) -> None:
    fresh_world_dir = golden_run(tmp_path).world_dir

    assert main(["evals", "generate", "--world", str(fresh_world_dir)]) == 0
    capsys.readouterr()

    assert main(["evals", "generate", "--world", str(fresh_world_dir)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


# -- list -----------------------------------------------------------------------


def test_evals_list_shows_counts(world_dir: Path, capsys: Any) -> None:
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()

    assert main(["evals", "list", "--world", str(world_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["questions"]) == payload["counts"]["source"]["generated"]
    assert sum(payload["counts"]["reasoning_type"].values()) == len(payload["questions"])


# -- score matches bench score (same numbers, via the shared score() function) --


def test_evals_score_matches_direct_score_call(
    world_dir: Path, capsys: Any, tmp_path: Path
) -> None:
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()

    benchmark = Benchmark.read_jsonl(world_dir / "evals" / "questions.jsonl")
    pred_path = tmp_path / "pred.jsonl"
    Predictions.from_mapping({p.id: list(p.expected_ids) for p in benchmark}).write_jsonl(pred_path)

    assert main(["evals", "score", "--world", str(world_dir), "--pred", str(pred_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    expected = score(benchmark, Predictions.read_jsonl(pred_path))
    assert payload["overall"]["macro_f1"] == expected.overall.macro_f1
    assert payload["overall"]["count"] == expected.overall.count
    assert payload["overall"]["macro_f1"] == 1.0  # perfect predictions
    assert set(payload["by_reasoning_type"]) == set(expected.by_reasoning_type)
    assert len(payload["items"]) == len(benchmark)


def test_evals_score_matches_bench_score_numbers(
    world_dir: Path, capsys: Any, tmp_path: Path
) -> None:
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()

    benchmark = Benchmark.read_jsonl(world_dir / "evals" / "questions.jsonl")
    pred_path = tmp_path / "pred2.jsonl"
    # An imperfect prediction set (half right) so overall isn't trivially 1.0.
    rows = {}
    for i, pair in enumerate(benchmark):
        rows[pair.id] = list(pair.expected_ids) if i % 2 == 0 else []
    Predictions.from_mapping(rows).write_jsonl(pred_path)

    assert main(["evals", "score", "--world", str(world_dir), "--pred", str(pred_path)]) == 0
    evals_payload = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "bench",
                "score",
                "--bench",
                str(world_dir / "evals" / "questions.jsonl"),
                "--pred",
                str(pred_path),
            ]
        )
        == 0
    )
    bench_text = capsys.readouterr().out
    assert f"F1={evals_payload['overall']['macro_f1']:.3f}" in bench_text


# -- add / remove -----------------------------------------------------------------


def test_evals_add_then_remove_round_trip(world_dir: Path, capsys: Any, tmp_path: Path) -> None:
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()

    node_id = json.loads((world_dir / "kg" / "nodes.jsonl").read_text().splitlines()[0])["id"]
    proposals_path = tmp_path / "proposals.json"
    proposals_path.write_text(
        json.dumps(
            [
                {
                    "question": f"Who is {node_id} (cli test)?",
                    "reasoning_type": "direct_relation",
                    "expected_ids": [node_id],
                },
                {"question": "bad", "reasoning_type": "not-a-type", "expected_ids": [node_id]},
            ]
        ),
        encoding="utf-8",
    )

    assert main(["evals", "add", "--world", str(world_dir), "--file", str(proposals_path)]) == 0
    added_payload = json.loads(capsys.readouterr().out)
    assert len(added_payload["added"]) == 1
    assert len(added_payload["rejected"]) == 1
    new_id = added_payload["added"][0]["id"]

    assert main(["evals", "remove", "--world", str(world_dir), "--ids", new_id]) == 0
    removed_payload = json.loads(capsys.readouterr().out)
    assert removed_payload["removed"] == [new_id]


def test_evals_remove_refuses_generated_questions(world_dir: Path, capsys: Any) -> None:
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()
    benchmark = Benchmark.read_jsonl(world_dir / "evals" / "questions.jsonl")
    generated_id = next(p.id for p in benchmark)

    assert main(["evals", "remove", "--world", str(world_dir), "--ids", generated_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == []
    assert payload["not_removed"] == [generated_id]


# -- sample -----------------------------------------------------------------------


def test_evals_sample_deterministic_and_stratified(world_dir: Path, capsys: Any) -> None:
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()

    assert (
        main(["evals", "sample", "--world", str(world_dir), "--fraction", "0.25", "--seed", "3"])
        == 0
    )
    first = json.loads(capsys.readouterr().out)

    assert (
        main(["evals", "sample", "--world", str(world_dir), "--fraction", "0.25", "--seed", "3"])
        == 0
    )
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert first["count"] == len(first["ids"])
    assert first["ids"] == sorted(first["ids"])


# -- run: clean error without a key -------------------------------------------------


def test_evals_run_clean_error_without_key(
    world_dir: Path, capsys: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    main(["evals", "generate", "--world", str(world_dir), "--force"])
    capsys.readouterr()
    benchmark = Benchmark.read_jsonl(world_dir / "evals" / "questions.jsonl")
    first_id = next(p.id for p in benchmark)

    out_dir = tmp_path / "eval-out"
    rc = main(
        [
            "evals",
            "run",
            "--world",
            str(world_dir),
            "--runner",
            "graph",
            "--out",
            str(out_dir),
            "--ids",
            first_id,
        ]
    )

    assert rc == 2
    stdout_lines = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(stdout_lines[-1])
    assert payload["kind"] == "error"
    assert "ANTHROPIC_API_KEY" in payload["error"]
    assert not out_dir.exists()
