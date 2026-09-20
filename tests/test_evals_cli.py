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
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("evals-cli-golden")
    result = golden_run(out)
    return result.run_dir


# -- generate matches bench generate, byte for byte ----------------------------


def test_evals_generate_matches_bench_generate_byte_for_byte(
    run_dir: Path, capsys: Any, tmp_path: Path
) -> None:
    bench_out = tmp_path / "bench.jsonl"
    assert main(["bench", "generate", "--run", str(run_dir), "-o", str(bench_out)]) == 0
    capsys.readouterr()

    assert main(["evals", "generate", "--run", str(run_dir), "--force"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True

    evals_path = run_dir / "evals" / "questions.jsonl"
    assert evals_path.read_bytes() == bench_out.read_bytes()


def test_evals_generate_refuses_to_overwrite_without_force(tmp_path: Path, capsys: Any) -> None:
    fresh_run_dir = golden_run(tmp_path).run_dir

    assert main(["evals", "generate", "--run", str(fresh_run_dir)]) == 0
    capsys.readouterr()

    assert main(["evals", "generate", "--run", str(fresh_run_dir)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


# -- list -----------------------------------------------------------------------


def test_evals_list_shows_counts(run_dir: Path, capsys: Any) -> None:
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()

    assert main(["evals", "list", "--run", str(run_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["questions"]) == payload["counts"]["source"]["generated"]
    assert sum(payload["counts"]["reasoning_type"].values()) == len(payload["questions"])


# -- score matches bench score (same numbers, via the shared score() function) --


def test_evals_score_matches_direct_score_call(run_dir: Path, capsys: Any, tmp_path: Path) -> None:
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()

    benchmark = Benchmark.read_jsonl(run_dir / "evals" / "questions.jsonl")
    pred_path = tmp_path / "pred.jsonl"
    Predictions.from_mapping({p.id: list(p.expected_ids) for p in benchmark}).write_jsonl(pred_path)

    assert main(["evals", "score", "--run", str(run_dir), "--pred", str(pred_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    expected = score(benchmark, Predictions.read_jsonl(pred_path))
    assert payload["overall"]["macro_f1"] == expected.overall.macro_f1
    assert payload["overall"]["count"] == expected.overall.count
    assert payload["overall"]["macro_f1"] == 1.0  # perfect predictions
    assert set(payload["by_reasoning_type"]) == set(expected.by_reasoning_type)
    assert len(payload["items"]) == len(benchmark)


def test_evals_score_matches_bench_score_numbers(
    run_dir: Path, capsys: Any, tmp_path: Path
) -> None:
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()

    benchmark = Benchmark.read_jsonl(run_dir / "evals" / "questions.jsonl")
    pred_path = tmp_path / "pred2.jsonl"
    # An imperfect prediction set (half right) so overall isn't trivially 1.0.
    rows = {}
    for i, pair in enumerate(benchmark):
        rows[pair.id] = list(pair.expected_ids) if i % 2 == 0 else []
    Predictions.from_mapping(rows).write_jsonl(pred_path)

    assert main(["evals", "score", "--run", str(run_dir), "--pred", str(pred_path)]) == 0
    evals_payload = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "bench",
                "score",
                "--bench",
                str(run_dir / "evals" / "questions.jsonl"),
                "--pred",
                str(pred_path),
            ]
        )
        == 0
    )
    bench_text = capsys.readouterr().out
    assert f"F1={evals_payload['overall']['macro_f1']:.3f}" in bench_text


# -- add / remove -----------------------------------------------------------------


def test_evals_add_then_remove_round_trip(run_dir: Path, capsys: Any, tmp_path: Path) -> None:
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()

    node_id = json.loads((run_dir / "kg" / "nodes.jsonl").read_text().splitlines()[0])["id"]
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

    assert main(["evals", "add", "--run", str(run_dir), "--file", str(proposals_path)]) == 0
    added_payload = json.loads(capsys.readouterr().out)
    assert len(added_payload["added"]) == 1
    assert len(added_payload["rejected"]) == 1
    new_id = added_payload["added"][0]["id"]

    assert main(["evals", "remove", "--run", str(run_dir), "--ids", new_id]) == 0
    removed_payload = json.loads(capsys.readouterr().out)
    assert removed_payload["removed"] == [new_id]


def test_evals_remove_refuses_generated_questions(run_dir: Path, capsys: Any) -> None:
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()
    benchmark = Benchmark.read_jsonl(run_dir / "evals" / "questions.jsonl")
    generated_id = next(p.id for p in benchmark)

    assert main(["evals", "remove", "--run", str(run_dir), "--ids", generated_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == []
    assert payload["not_removed"] == [generated_id]


# -- sample -----------------------------------------------------------------------


def test_evals_sample_deterministic_and_stratified(run_dir: Path, capsys: Any) -> None:
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()

    assert (
        main(["evals", "sample", "--run", str(run_dir), "--fraction", "0.25", "--seed", "3"]) == 0
    )
    first = json.loads(capsys.readouterr().out)

    assert (
        main(["evals", "sample", "--run", str(run_dir), "--fraction", "0.25", "--seed", "3"]) == 0
    )
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert first["count"] == len(first["ids"])
    assert first["ids"] == sorted(first["ids"])


# -- run: clean error without a key -------------------------------------------------


def test_evals_run_clean_error_without_key(
    run_dir: Path, capsys: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    main(["evals", "generate", "--run", str(run_dir), "--force"])
    capsys.readouterr()
    benchmark = Benchmark.read_jsonl(run_dir / "evals" / "questions.jsonl")
    first_id = next(p.id for p in benchmark)

    out_dir = tmp_path / "eval-out"
    rc = main(
        [
            "evals",
            "run",
            "--run",
            str(run_dir),
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
