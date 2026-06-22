"""Determinism tests for sub-seed derivation and split-RNG sub-streams (D10/D26)."""

from __future__ import annotations

from enterprise_sim.core.config import SeedContext, derive_subseed, substream


def test_subseed_is_deterministic() -> None:
    a = derive_subseed(42, "scenario-1", "process-a", "instance-3", "step-7")
    b = derive_subseed(42, "scenario-1", "process-a", "instance-3", "step-7")
    assert a == b


def test_subseed_is_a_nonnegative_64bit_int() -> None:
    value = derive_subseed(42, "scenario-1", "step-7")
    assert isinstance(value, int)
    assert 0 <= value < 2**64


def test_different_parts_give_different_subseeds() -> None:
    base = derive_subseed(42, "scenario-1", "process-a")
    assert base != derive_subseed(42, "scenario-1", "process-b")
    assert base != derive_subseed(42, "scenario-2", "process-a")
    assert base != derive_subseed(99, "scenario-1", "process-a")


def test_part_boundaries_are_unambiguous() -> None:
    # The unit separator must keep regroupings of the same characters distinct.
    assert derive_subseed(0, "a", "bc") != derive_subseed(0, "ab", "c")
    assert derive_subseed(0, "a", "b") != derive_subseed(0, "ab")


def test_root_seed_is_normalised_through_int() -> None:
    # bool is an int subtype; True must derive identically to 1.
    assert derive_subseed(True, "x") == derive_subseed(1, "x")


def test_subseed_is_stable_across_processes() -> None:
    # Golden value pins the algorithm: a change to the hashing scheme breaks this
    # on purpose, since it would silently invalidate every reproducible run.
    assert derive_subseed(42, "scenario-1", "process-a", "instance-3", "step-7") == (
        4244698303991013949
    )


def test_substream_sequences_reproduce() -> None:
    first = substream(42, "scenario-1", "step-7")
    second = substream(42, "scenario-1", "step-7")
    assert [first.random() for _ in range(5)] == [second.random() for _ in range(5)]


def test_substreams_with_different_parts_diverge() -> None:
    a = substream(42, "scenario-1")
    b = substream(42, "scenario-2")
    assert [a.random() for _ in range(5)] != [b.random() for _ in range(5)]


def test_seed_context_matches_module_functions() -> None:
    ctx = SeedContext(42)
    assert ctx.derive("scenario-1", "step-7") == derive_subseed(42, "scenario-1", "step-7")
    assert [ctx.rng("s").random() for _ in range(3)] == [
        substream(42, "s").random() for _ in range(3)
    ]


def test_seed_context_child_is_nested_namespace() -> None:
    ctx = SeedContext(42)
    child = ctx.child("scenario-1")
    # child's root is the derived sub-seed...
    assert child.root == ctx.derive("scenario-1")
    # ...and nesting is a distinct namespace from one-shot derivation.
    assert child.derive("step-7") != ctx.derive("scenario-1", "step-7")
    # nesting is itself deterministic
    assert ctx.child("scenario-1").derive("step-7") == child.derive("step-7")


def test_seed_context_is_frozen_and_hashable() -> None:
    ctx = SeedContext(7)
    assert hash(ctx) == hash(SeedContext(7))
