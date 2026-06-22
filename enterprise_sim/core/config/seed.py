"""Seed / determinism: one root seed, deterministic split-RNG sub-streams.

Determinism in Enterprise Sim is *structural*, not byte-identical (D10): LLM
output is nondeterministic, but the *structure* — staffing, schedule, event log,
ids — must reproduce exactly across runs. The mechanism is a single root seed
that threads through the whole run; each plugin / scenario / process / producer
derives a stable **sub-seed** from the root plus a tuple of identifying parts
(D26: ``hash(root, scenario, process, instance, step)``). Every stochastic draw
pulls from a :class:`random.Random` seeded by such a sub-seed, so two runs with
the same root produce the same draws regardless of evaluation order or
concurrency (only Layer C rendering parallelizes — see ARCHITECTURE.md §15).

The derivation must be stable across processes and Python invocations, so it uses
:mod:`hashlib` (BLAKE2b) rather than the built-in :func:`hash`, which is salted
per-process by ``PYTHONHASHSEED``.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

# Unit separator placed between encoded parts so that the boundaries between
# parts are unambiguous: derive(r, "a", "bc") must differ from derive(r, "ab",
# "c"). Without a separator both would hash the byte string ``abc``.
_SEP = b"\x1f"

# 64-bit sub-seeds: wide enough to avoid collisions across a run, and a natural
# fit for seeding ``random.Random`` / numpy generators downstream.
_DIGEST_SIZE = 8


def derive_subseed(root_seed: int, *parts: object) -> int:
    """Derive a stable 64-bit sub-seed from ``root_seed`` and identifying parts.

    The derivation is a pure function of its arguments and is stable across
    processes and Python runs (it does not use the salted built-in ``hash``).
    Parts are stringified and separated by a unit-separator byte so that
    different groupings of the same characters never collide.

    Args:
        root_seed: The run's root seed.
        *parts: Identifying components of the sub-stream, e.g. scenario id,
            process name, instance id, step id. Any object is accepted and
            rendered via ``str``; callers should pass stable identifiers.

    Returns:
        A non-negative 64-bit integer suitable for seeding a PRNG.
    """
    digest = hashlib.blake2b(digest_size=_DIGEST_SIZE)
    # Normalise the root through int() so 5 and 5.0 derive identically and a
    # bool can never sneak in as a distinct key.
    digest.update(str(int(root_seed)).encode("utf-8"))
    for part in parts:
        digest.update(_SEP)
        digest.update(str(part).encode("utf-8"))
    return int.from_bytes(digest.digest(), "big")


def substream(root_seed: int, *parts: object) -> random.Random:
    """Return a :class:`random.Random` seeded by ``derive_subseed(root, *parts)``.

    Two calls with identical arguments yield independent generators that emit
    identical sequences — the basis for reproducible stochastic placement.
    """
    return random.Random(derive_subseed(root_seed, *parts))


@dataclass(frozen=True)
class SeedContext:
    """A hierarchical seed scope that threads the root seed through a run.

    A :class:`SeedContext` wraps a root seed and derives sub-seeds and PRNGs
    from it. :meth:`child` returns a *new* context whose root is itself a
    derived sub-seed, letting callers build nested scopes
    (``ctx.child(scenario).child(process)``) without manually threading the
    full part tuple at every level. Nesting and one-shot derivation are
    *distinct* namespaces — ``ctx.child(a).derive(b)`` is not the same sub-seed
    as ``ctx.derive(a, b)`` — so a given scope should pick one style and stay
    consistent; both are equally deterministic.

    Frozen and hashable, so a context can be stored on immutable plan nodes and
    snapshotted alongside the config.
    """

    root: int

    def derive(self, *parts: object) -> int:
        """Derive a sub-seed under this context (see :func:`derive_subseed`)."""
        return derive_subseed(self.root, *parts)

    def rng(self, *parts: object) -> random.Random:
        """Return a PRNG for the sub-stream identified by ``parts``."""
        return substream(self.root, *parts)

    def child(self, *parts: object) -> SeedContext:
        """Return a nested context rooted at ``self.derive(*parts)``."""
        return SeedContext(self.derive(*parts))
