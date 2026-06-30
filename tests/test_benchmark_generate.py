"""Generator tests (esim-uzc.2): determinism, id validity, reasoning spread, CLI.

The generator turns the gold KG into a Q/A benchmark; these tests pin the
acceptance criteria of esim-uzc.2:

* every ``expected_id`` resolves to a real gold-KG node;
* the benchmark is byte-stable — the same gold run yields identical JSONL, both
  for one world generated twice and across two independent golden runs;
* it spans the documented reasoning types with a healthy pair count; and
* ``enterprise-sim bench generate`` writes that benchmark to a file / stdout.

Most tests share one golden run via a module-scoped fixture (executing the run is
the expensive part); :func:`test_byte_stable_across_independent_runs` pays for a
second run to prove cross-run reproducibility.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark import REASONING_TYPES
from enterprise_sim.benchmark.fixtures import golden_run
from enterprise_sim.benchmark.generate import (
    build_benchmark,
    generate,
    load_groundings,
    load_world_from_run,
)
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.cli import main
from enterprise_sim.core.world import World


@pytest.fixture(scope="module")
def gold() -> tuple[World, dict[str, list[str]]]:
    """A single golden run shared across tests: its gold world and grounding map."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="esim-bench-test-") as tmp:
        result = golden_run(tmp)
        world = result.world
        groundings = load_groundings(result.run_dir, world)
        return world, groundings


@pytest.fixture(scope="module")
def benchmark(gold: tuple[World, dict[str, list[str]]]) -> Benchmark:
    """The benchmark built from the shared golden run."""
    world, groundings = gold
    return build_benchmark(world, groundings)


# -- acceptance: id validity ------------------------------------------------


def test_every_expected_id_is_a_real_node(
    benchmark: Benchmark, gold: tuple[World, dict[str, list[str]]]
) -> None:
    world, _ = gold
    for pair in benchmark:
        assert pair.expected_ids, f"{pair.id} has no expected answer"
        for node_id in pair.expected_ids:
            assert node_id in world, f"{pair.id} references non-node {node_id!r}"


def test_pairs_are_well_formed(benchmark: Benchmark) -> None:
    ids = [pair.id for pair in benchmark]
    assert len(ids) == len(set(ids)), "pair ids must be unique"
    for pair in benchmark:
        assert isinstance(pair, QAPair)
        assert pair.id.startswith("qa-")
        assert pair.question.strip()
        assert pair.reasoning_type in REASONING_TYPES


# -- acceptance: scale and reasoning spread ---------------------------------


def test_at_least_twenty_pairs_over_four_reasoning_types(benchmark: Benchmark) -> None:
    assert len(benchmark) >= 20
    present = {pair.reasoning_type for pair in benchmark}
    assert len(present) >= 4


def test_spans_every_documented_reasoning_type(benchmark: Benchmark) -> None:
    present = {pair.reasoning_type for pair in benchmark}
    assert present == set(REASONING_TYPES)


# -- acceptance: determinism ------------------------------------------------


def test_deterministic_for_identical_inputs(
    benchmark: Benchmark, gold: tuple[World, dict[str, list[str]]]
) -> None:
    world, groundings = gold
    again = build_benchmark(world, groundings)
    assert again.to_jsonl() == benchmark.to_jsonl()


def test_byte_stable_across_independent_runs() -> None:
    first = generate().to_jsonl()
    second = generate().to_jsonl()
    assert first == second
    assert first, "benchmark must not be empty"


def test_pairs_are_sorted_stably(benchmark: Benchmark) -> None:
    keys = [
        (pair.reasoning_type, pair.qtype, pair.question, *pair.expected_ids) for pair in benchmark
    ]
    assert keys == sorted(keys)


# -- spot checks: the answers are the right facts ---------------------------


def _by_question(benchmark: Benchmark, needle: str) -> QAPair:
    matches = [pair for pair in benchmark if needle in pair.question]
    assert len(matches) == 1, f"expected exactly one question containing {needle!r}"
    return matches[0]


def test_direct_relation_reports_to(benchmark: Benchmark) -> None:
    pair = _by_question(benchmark, "Who does Cleo Costa report to?")
    assert pair.reasoning_type == "direct_relation"
    assert pair.expected_ids == ("person:ben-cho",)
    assert pair.expected_label == "Ben Cho"


def test_transitive_management_chain_is_multi_hop(benchmark: Benchmark) -> None:
    pair = _by_question(benchmark, "Who is in Cleo Costa's management chain")
    assert pair.reasoning_type == "transitive"
    # Cleo -> Ben Cho -> Yuki Quintero: the skip-level manager is included.
    assert pair.expected_ids == ("person:ben-cho", "person:yuki-quintero")
    assert pair.difficulty == "hard"


def test_transitive_department(benchmark: Benchmark) -> None:
    pair = _by_question(benchmark, "Which department is Ben Cho in?")
    assert pair.reasoning_type == "transitive"
    assert pair.expected_ids == ("dept:engineering",)


def test_aggregation_count_matches_member_set(benchmark: Benchmark) -> None:
    pair = _by_question(benchmark, "How many people are on Quality Engineering?")
    assert pair.reasoning_type == "aggregation"
    assert pair.expected_label == str(len(pair.expected_ids))


def test_provenance_answers_are_artifacts(
    benchmark: Benchmark, gold: tuple[World, dict[str, list[str]]]
) -> None:
    world, _ = gold
    provenance = [pair for pair in benchmark if pair.reasoning_type == "provenance"]
    assert provenance
    for pair in provenance:
        for node_id in pair.expected_ids:
            node = world.get_node(node_id)
            assert node is not None and node.type == "Artifact"


def test_goal_tree_present(benchmark: Benchmark) -> None:
    goal_tree = [pair for pair in benchmark if pair.reasoning_type == "goal_tree"]
    assert goal_tree
    for pair in goal_tree:
        assert "goal" in pair.question.lower()


# -- loaders ----------------------------------------------------------------


def test_load_world_from_run_matches_in_memory_world(tmp_path: Path) -> None:
    result = golden_run(tmp_path / "out")
    reloaded = load_world_from_run(result.run_dir)
    assert reloaded.node_count == result.world.node_count
    assert reloaded.edge_count == result.world.edge_count


def test_generate_from_run_dir_equals_fresh_run(tmp_path: Path) -> None:
    result = golden_run(tmp_path / "out")
    from_dir = generate(result.run_dir).to_jsonl()
    fresh = generate().to_jsonl()
    assert from_dir == fresh


# -- CLI --------------------------------------------------------------------


def test_cli_generate_writes_jsonl_file(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "bench.jsonl"
    assert main(["bench", "generate", "-o", str(out)]) == 0
    loaded = Benchmark.read_jsonl(out)
    assert len(loaded) >= 20
    err = capsys.readouterr().err
    assert "reasoning types" in err


def test_cli_generate_from_run_dir(tmp_path: Path) -> None:
    result = golden_run(tmp_path / "out")
    out = tmp_path / "bench.jsonl"
    assert main(["bench", "generate", "--run", str(result.run_dir), "-o", str(out)]) == 0
    assert Benchmark.read_jsonl(out).to_jsonl() == generate().to_jsonl()


def test_cli_generate_to_stdout(capsys: Any) -> None:
    assert main(["bench", "generate"]) == 0
    captured = capsys.readouterr()
    assert Benchmark.from_jsonl(captured.out).to_jsonl() == generate().to_jsonl()
