"""Deterministic eval-question sampling (EXPLORER_EVALS.md §2/§3, ``evals sample``).

**This module is the reference implementation a TypeScript port
(``src/sidecar/evals/sample.ts``) must match bit-for-bit** — the design doc
requires the two to agree for the same ``(ids, fraction, seed, stratify)``
input, which ``tests/fixtures/evals_sample_parity.json`` (generated from this
module) exists to pin. Two things must be ported exactly:

1. **The PRNG — mulberry32.** A tiny, public-domain 32-bit generator (NOT
   Python's :mod:`random`, which has no portable cross-language spec). The
   canonical JavaScript reference is::

       function mulberry32(a) {
         return function() {
           a |= 0; a = a + 0x6D2B79F5 | 0;
           var t = Math.imul(a ^ a >>> 15, 1 | a);
           t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
           return ((t ^ t >>> 14) >>> 0) / 4294967296;
         }
       }

   Given the generator's current 32-bit unsigned state ``a`` (``&`` denotes the
   ``mod 2**32`` mask every step below applies, matching JS's implicit
   ``|0``/``Math.imul``/``>>> 0`` coercions), one draw is::

       a      = (a + 0x6D2B79F5) & 0xFFFFFFFF          # new state, carried to the next draw
       t0     = ((a ^ (a >> 15)) * (a | 1)) & 0xFFFFFFFF
       inner  = ((t0 ^ (t0 >> 7)) * (t0 | 61)) & 0xFFFFFFFF
       t1     = ((t0 + inner) & 0xFFFFFFFF) ^ t0        # note: XORs against t0, the PRE-inner value
       out    = (t1 ^ (t1 >> 14)) & 0xFFFFFFFF           # the uint32 draw
       float  = out / 4294967296                          # in [0, 1)

   See :class:`Mulberry32` for the literal implementation of exactly these
   steps. ``a`` is reassigned each call and carried to the next — the
   generator is a stateful stream seeded once, not a pure function of an
   index.

2. **The sampling algorithm — seeded Fisher-Yates over *sorted* ids.**
   :func:`seeded_shuffle` always starts from ``sorted(ids)`` (lexicographic
   byte order), never the caller's order, so the result is a pure function of
   the id *set* and the seed. It then runs the standard Fisher-Yates shuffle
   (``for i from n-1 down to 1: swap(i, floor(next_float() * (i+1)))``) driven
   by one :class:`Mulberry32` seeded with the integer ``seed``.
   :func:`sample_ids` takes the first ``ceil(fraction * n)`` ids of that
   shuffle and returns them **sorted** (a stable, order-independent selection
   set — the shuffle only determines *which* ids are chosen, not the output
   order). ``fraction <= 0`` -> ``[]``; ``fraction >= 1`` -> every id, sorted.

   **Stratified** sampling (:func:`sample`, ``stratify=True``) groups the
   input ``QAPair``\\s by ``reasoning_type``, and for each type (iterated in
   sorted-type order) calls :func:`sample_ids` **independently** — i.e. with a
   *fresh* ``Mulberry32(seed)`` per group (not a single generator stream
   shared/continued across groups) — taking ``ceil(fraction * n_type)`` from
   each, then unions and sorts the per-group results. Seeding fresh per group
   means the presence or absence of one reasoning type never perturbs another
   type's draw, and a group's result depends only on ``(that group's id set,
   fraction, seed)``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from enterprise_sim.benchmark.schema import QAPair

_MASK32 = 0xFFFFFFFF


class Mulberry32:
    """A 32-bit deterministic PRNG (public-domain "mulberry32").

    Ported bit-for-bit from the canonical JavaScript reference so a
    TypeScript port draws an identical sequence for the same seed. All
    arithmetic is performed modulo ``2**32`` (Python has arbitrary-precision
    ints, so every operation below is explicitly masked with ``& 0xFFFFFFFF``
    to reproduce JavaScript's 32-bit ``|0``/``Math.imul``/``>>> 0``
    semantics) — a literal transcription is the point, not a "Pythonic"
    rewrite.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK32

    def next_uint32(self) -> int:
        """Advance the generator one step; return a uniform value in ``[0, 2**32)``."""
        a = (self._state + 0x6D2B79F5) & _MASK32
        self._state = a
        t0 = ((a ^ (a >> 15)) * (a | 1)) & _MASK32
        inner = ((t0 ^ (t0 >> 7)) * (t0 | 61)) & _MASK32
        t1 = ((t0 + inner) & _MASK32) ^ t0
        return (t1 ^ (t1 >> 14)) & _MASK32

    def next_float(self) -> float:
        """A uniform value in ``[0, 1)`` — ``next_uint32() / 2**32``."""
        return self.next_uint32() / 4294967296.0


def seeded_shuffle(ids: Iterable[str], seed: int) -> list[str]:
    """A deterministic Fisher-Yates shuffle of ``sorted(ids)``, seeded by :class:`Mulberry32`."""
    items = sorted(ids)
    rng = Mulberry32(seed)
    for i in range(len(items) - 1, 0, -1):
        j = int(rng.next_float() * (i + 1))
        items[i], items[j] = items[j], items[i]
    return items


def sample_ids(ids: Iterable[str], *, fraction: float, seed: int) -> list[str]:
    """The first ``ceil(fraction * n)`` ids of a seeded shuffle of ``sorted(ids)``, sorted."""
    items = list(ids)
    n = len(items)
    if n == 0 or fraction <= 0:
        return []
    if fraction >= 1:
        return sorted(items)
    k = math.ceil(fraction * n)
    return sorted(seeded_shuffle(items, seed)[:k])


def sample(
    pairs: Sequence[QAPair], *, fraction: float, seed: int, stratify: bool = False
) -> list[str]:
    """Deterministically sample a ``fraction`` of ``pairs``' ids (see module docstring)."""
    if not stratify:
        return sample_ids((p.id for p in pairs), fraction=fraction, seed=seed)

    by_type: dict[str, list[str]] = {}
    for pair in pairs:
        by_type.setdefault(pair.reasoning_type, []).append(pair.id)

    result: list[str] = []
    for reasoning_type in sorted(by_type):
        result.extend(sample_ids(by_type[reasoning_type], fraction=fraction, seed=seed))
    return sorted(result)


__all__ = ["Mulberry32", "sample", "sample_ids", "seeded_shuffle"]
