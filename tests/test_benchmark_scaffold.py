"""Benchmark scaffold tests (esim-uzc.1): QAPair schema, Benchmark JSONL, fixtures, CLI group.

Covers the package foundation the rest of the KG-QA benchmark builds on:

* :class:`QAPair` round-trips to/from a single JSONL line and validates its
  ``reasoning_type``;
* :class:`Benchmark` round-trips a collection through a ``.jsonl`` file;
* :func:`fixtures.load_gold_world` executes the golden run and returns a
  non-empty gold KG; and
* ``enterprise-sim bench --help`` lists the command group.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark import REASONING_TYPES, Benchmark, QAPair, fixtures
from enterprise_sim.cli import build_parser, main
from enterprise_sim.core.world import World


def _pair(**overrides: Any) -> QAPair:
    base: dict[str, Any] = {
        "id": "qa-0001",
        "question": "Who does Ada Lovelace report to?",
        "qtype": "who",
        "reasoning_type": "direct_relation",
        "expected_ids": ("person:charles-babbage",),
        "expected_label": "Charles Babbage",
        "difficulty": "easy",
    }
    base.update(overrides)
    return QAPair(**base)


# -- QAPair -----------------------------------------------------------------


def test_qapair_is_frozen() -> None:
    pair = _pair()
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.question = "mutated"  # type: ignore[misc]


def test_qapair_jsonl_round_trip() -> None:
    pair = _pair()
    line = pair.to_jsonl()
    assert "\n" not in line
    assert QAPair.from_jsonl(line) == pair


def test_qapair_round_trips_multiple_expected_ids_and_no_label() -> None:
    pair = _pair(
        expected_ids=("artifact:a", "artifact:b"),
        expected_label=None,
        reasoning_type="provenance",
    )
    assert QAPair.from_jsonl(pair.to_jsonl()) == pair
    assert isinstance(QAPair.from_jsonl(pair.to_jsonl()).expected_ids, tuple)


def test_qapair_normalizes_list_expected_ids_to_tuple() -> None:
    pair = QAPair(
        id="q",
        question="?",
        qtype="who",
        reasoning_type="direct_relation",
        expected_ids=["a", "b"],  # type: ignore[arg-type]
    )
    assert pair.expected_ids == ("a", "b")
    assert isinstance(pair.expected_ids, tuple)
    # Hashable now that expected_ids is a tuple.
    assert pair in {pair}


def test_qapair_rejects_unknown_reasoning_type() -> None:
    with pytest.raises(ValueError, match="reasoning_type"):
        _pair(reasoning_type="telepathy")


def test_all_reasoning_types_accepted() -> None:
    for reasoning in REASONING_TYPES:
        assert _pair(reasoning_type=reasoning).reasoning_type == reasoning


def test_reasoning_types_are_the_documented_set() -> None:
    assert REASONING_TYPES == {
        "direct_relation",
        "transitive",
        "provenance",
        "aggregation",
        "goal_tree",
    }


# -- Benchmark --------------------------------------------------------------


def test_benchmark_jsonl_file_round_trip(tmp_path: Path) -> None:
    bench = Benchmark.of(
        [
            _pair(id="qa-1"),
            _pair(id="qa-2", reasoning_type="aggregation", expected_label=None),
        ]
    )
    path = tmp_path / "bench.jsonl"
    bench.write_jsonl(path)

    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == 2

    loaded = Benchmark.read_jsonl(path)
    assert len(loaded) == 2
    assert loaded.pairs == bench.pairs
    assert [p.id for p in loaded] == ["qa-1", "qa-2"]


def test_empty_benchmark_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    Benchmark().write_jsonl(path)
    assert path.read_text(encoding="utf-8") == ""
    assert len(Benchmark.read_jsonl(path)) == 0


def test_benchmark_from_jsonl_skips_blank_lines() -> None:
    text = f"{_pair(id='a').to_jsonl()}\n\n{_pair(id='b').to_jsonl()}\n"
    bench = Benchmark.from_jsonl(text)
    assert [p.id for p in bench] == ["a", "b"]


# -- fixtures ---------------------------------------------------------------


def test_load_gold_world_returns_non_empty_kg() -> None:
    world = fixtures.load_gold_world()
    assert isinstance(world, World)
    assert world.node_count > 0
    assert world.edge_count > 0


def test_golden_run_lands_a_run_dir(tmp_path: Path) -> None:
    result = fixtures.golden_run(tmp_path / "out")
    assert result.run_dir.is_dir()
    assert result.world.node_count > 0


def test_golden_config_path_exists() -> None:
    assert fixtures.GOLDEN_CONFIG.is_file()


# -- CLI --------------------------------------------------------------------


def test_bench_group_is_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["bench"])
    assert args.command == "bench"
    assert args.func is not None


def test_bench_help_lists_the_group(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bench", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "bench" in out
    assert "generate" in out


def test_bench_without_subcommand_prints_help_and_returns_2(capsys: Any) -> None:
    assert main(["bench"]) == 2
    out = capsys.readouterr().out
    assert "generate" in out
