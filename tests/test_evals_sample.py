"""Tests for ``enterprise_sim.evals.sample``: the mulberry32 PRNG + seeded Fisher-Yates sampler.

Also asserts the ``tests/fixtures/evals_sample_parity.json`` fixture (generated
from this module, for a TypeScript port to test against) is internally
consistent with the live implementation — a change to the algorithm without
regenerating the fixture fails loudly here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from enterprise_sim.benchmark.schema import QAPair
from enterprise_sim.evals.sample import Mulberry32, sample, sample_ids, seeded_shuffle

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "evals_sample_parity.json"


# -- Mulberry32: matches the canonical JS reference (verified against node) ---


def test_mulberry32_matches_known_js_reference_seed_1() -> None:
    # Cross-checked against the canonical `mulberry32(1)` JS implementation via node.
    rng = Mulberry32(1)
    got = [rng.next_float() for _ in range(5)]
    expected = [
        0.6270739405881613,
        0.002735721180215478,
        0.5274470399599522,
        0.9810509674716741,
        0.9683778982143849,
    ]
    for g, e in zip(got, expected, strict=True):
        assert g == e


def test_mulberry32_is_deterministic() -> None:
    a = [Mulberry32(42).next_float() for _ in range(20)]
    b = [Mulberry32(42).next_float() for _ in range(20)]
    assert a == b


def test_mulberry32_different_seeds_diverge() -> None:
    a = [Mulberry32(1).next_float() for _ in range(10)]
    b = [Mulberry32(2).next_float() for _ in range(10)]
    assert a != b


def test_mulberry32_output_in_unit_interval() -> None:
    rng = Mulberry32(12345)
    for _ in range(1000):
        value = rng.next_float()
        assert 0.0 <= value < 1.0


# -- seeded_shuffle -------------------------------------------------------------


def test_seeded_shuffle_is_a_permutation_of_sorted_input() -> None:
    ids = ["z", "a", "m", "b"]
    shuffled = seeded_shuffle(ids, seed=1)
    assert sorted(shuffled) == sorted(ids)


def test_seeded_shuffle_ignores_caller_order() -> None:
    a = seeded_shuffle(["z", "a", "m"], seed=7)
    b = seeded_shuffle(["m", "z", "a"], seed=7)
    assert a == b  # both start from sorted(ids)


def test_seeded_shuffle_deterministic_for_seed() -> None:
    ids = [f"id{i}" for i in range(30)]
    assert seeded_shuffle(ids, seed=99) == seeded_shuffle(ids, seed=99)


# -- sample_ids -------------------------------------------------------------------


def test_sample_ids_empty_input() -> None:
    assert sample_ids([], fraction=0.5, seed=1) == []


def test_sample_ids_zero_fraction() -> None:
    assert sample_ids(["a", "b"], fraction=0.0, seed=1) == []


def test_sample_ids_full_fraction_returns_everything_sorted() -> None:
    assert sample_ids(["c", "a", "b"], fraction=1.0, seed=1) == ["a", "b", "c"]


def test_sample_ids_takes_ceil_fraction() -> None:
    ids = [f"id{i}" for i in range(10)]
    result = sample_ids(ids, fraction=0.25, seed=3)
    assert len(result) == math.ceil(0.25 * 10)


def test_sample_ids_is_sorted_output() -> None:
    ids = [f"id{i}" for i in range(20)]
    result = sample_ids(ids, fraction=0.5, seed=3)
    assert result == sorted(result)


def test_sample_ids_deterministic() -> None:
    ids = [f"id{i}" for i in range(50)]
    a = sample_ids(ids, fraction=0.3, seed=17)
    b = sample_ids(ids, fraction=0.3, seed=17)
    assert a == b


def test_sample_ids_depends_only_on_id_set_not_order() -> None:
    ids = [f"id{i}" for i in range(20)]
    import random

    shuffled = list(ids)
    random.Random(0).shuffle(shuffled)
    assert sample_ids(ids, fraction=0.4, seed=5) == sample_ids(shuffled, fraction=0.4, seed=5)


# -- stratified sample() -----------------------------------------------------------


def _pairs(id_types: dict[str, str]) -> list[QAPair]:
    return [
        QAPair(id=i, question="q", qtype="what", reasoning_type=t, expected_ids=("x",))
        for i, t in id_types.items()
    ]


def test_stratified_sample_takes_ceil_per_type() -> None:
    id_types = {f"a{i}": "direct_relation" for i in range(4)} | {
        f"b{i}": "transitive" for i in range(3)
    }
    result = sample(_pairs(id_types), fraction=0.5, seed=1, stratify=True)
    a_selected = [i for i in result if id_types[i] == "direct_relation"]
    b_selected = [i for i in result if id_types[i] == "transitive"]
    assert len(a_selected) == math.ceil(0.5 * 4)
    assert len(b_selected) == math.ceil(0.5 * 3)


def test_stratified_sample_result_is_sorted() -> None:
    id_types = {f"z{i}": "direct_relation" for i in range(5)} | {
        f"a{i}": "transitive" for i in range(5)
    }
    result = sample(_pairs(id_types), fraction=0.4, seed=2, stratify=True)
    assert result == sorted(result)


def test_non_stratified_sample_matches_sample_ids() -> None:
    id_types = {f"id{i}": "direct_relation" for i in range(10)}
    pairs = _pairs(id_types)
    assert sample(pairs, fraction=0.3, seed=8, stratify=False) == sample_ids(
        [p.id for p in pairs], fraction=0.3, seed=8
    )


# -- fixture parity (the artifact a TS port is tested against) ----------------------


def test_fixture_exists_and_matches_live_implementation() -> None:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 5

    for case in cases:
        if case["stratify"]:
            pairs = _pairs(case["reasoning_types"])
            got = sample(pairs, fraction=case["fraction"], seed=case["seed"], stratify=True)
        else:
            got = sample_ids(case["ids"], fraction=case["fraction"], seed=case["seed"])
        assert got == case["expected_ids"], case["name"]
