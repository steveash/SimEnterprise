"""The engagement code every walled project carries (decision record 0036).

A client engagement has a reference code ("KQT-4821") that its documents quote and no
other engagement shares, the way a legal matter number or a client account id works.
It gives every walled project at least one decisive value, whatever its other
attributes' banks hold: the ledger draws it for walled projects only, unique among
them (``facts.ledger._avoid``), and it never changes.
"""

from __future__ import annotations

import random

from enterprise_sim.facts.catalog import Attribute, Context

__all__ = ["ENGAGEMENT_CODE"]

_LETTERS = "BCDFGHJKLMNPQRSTVWXZ"  # consonants only: a code never spells a word


def _draw(rng: random.Random, ctx: Context) -> str:
    del ctx
    letters = "".join(rng.choice(_LETTERS) for _ in range(3))
    return f"{letters}-{rng.randint(1000, 9999)}"


def _keep(rng: random.Random, old: str, ctx: Context) -> str:
    del rng, ctx
    return old  # a reference code is fixed for the engagement's life


ENGAGEMENT_CODE = Attribute(
    name="engagement_code",
    label="engagement code",
    draw=_draw,
    mutate=_keep,
    current=(
        "{subject} is filed under engagement code {value}.",
        "The engagement code for {subject} is {value}.",
    ),
    update="The engagement code for {subject} changed from {old} to {value}.",
    question="What is the engagement code of {subject}?",
    volatile=0.0,
)
