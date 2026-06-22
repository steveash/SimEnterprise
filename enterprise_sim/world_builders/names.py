"""Deterministic name + slug helpers for Layer A world building.

Layer A invents a believable org out of thin air, so it needs names — for people,
for the company's goals, for its initiatives — that look human yet reproduce
exactly from the run's seed (D10). This module is the small, dependency-free name
substrate the builder draws from: fixed word banks plus a slugifier and a
collision-stable id allocator.

Nothing here draws randomness on its own; every pick is made by the caller's
seeded :class:`random.Random` (see :mod:`enterprise_sim.core.config.seed`), so the
*same* seed and draw order always yield the *same* names and ids.
"""

from __future__ import annotations

import random
import re

__all__ = ["FIRST_NAMES", "LAST_NAMES", "SlugAllocator", "pick", "slugify"]

# Curated, intentionally diverse name banks. Large enough that small orgs rarely
# collide; collisions that do happen are disambiguated by :class:`SlugAllocator`.
FIRST_NAMES: tuple[str, ...] = (
    "Ada",
    "Ben",
    "Chen",
    "Diego",
    "Elena",
    "Farah",
    "Grace",
    "Hiro",
    "Ines",
    "Jamal",
    "Kira",
    "Liam",
    "Mira",
    "Noor",
    "Omar",
    "Priya",
    "Quinn",
    "Rosa",
    "Sven",
    "Tara",
    "Ugo",
    "Vera",
    "Wes",
    "Xian",
    "Yuki",
    "Zara",
    "Aria",
    "Bodhi",
    "Cleo",
    "Dario",
    "Esme",
    "Felix",
    "Gita",
    "Hugo",
    "Isla",
    "Jonas",
    "Kemal",
    "Lena",
    "Marco",
    "Nadia",
)
LAST_NAMES: tuple[str, ...] = (
    "Acosta",
    "Bauer",
    "Cho",
    "Diaz",
    "Engel",
    "Fontaine",
    "Greco",
    "Haddad",
    "Ibarra",
    "Jain",
    "Kowalski",
    "Lindqvist",
    "Moreau",
    "Nakamura",
    "Okafor",
    "Pereira",
    "Quintero",
    "Rossi",
    "Singh",
    "Tanaka",
    "Ueda",
    "Volkov",
    "Wang",
    "Xu",
    "Yamamoto",
    "Zhang",
    "Andersson",
    "Brandt",
    "Costa",
    "Dubois",
    "Eriksen",
    "Ferreira",
    "Gallo",
    "Hassan",
    "Ito",
    "Jensen",
    "Kaur",
    "Laurent",
    "Mehta",
    "Novak",
)


def slugify(text: str) -> str:
    """Return a lowercase, ``-``-separated, filesystem/id-safe slug of ``text``.

    Collapses any run of non-alphanumeric characters to a single hyphen and
    trims leading/trailing hyphens. An empty or all-symbol input yields ``"x"``
    so an id is never blank.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "x"


def pick(rng: random.Random, options: tuple[str, ...]) -> str:
    """Return one element of ``options`` chosen by the seeded ``rng``."""
    return options[rng.randrange(len(options))]


class SlugAllocator:
    """Hands out unique slugs, disambiguating collisions deterministically.

    The first request for a given base slug returns it unchanged; subsequent
    requests for the same base get ``-2``, ``-3``, … suffixes. Because the
    builder allocates in a fixed (seed-derived) order, the same inputs always
    produce the same final ids — two ``Ada Acosta``\\ s become ``ada-acosta`` and
    ``ada-acosta-2`` reproducibly.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def allocate(self, base: str) -> str:
        """Return a unique slug derived from ``base`` (suffixing ``-N`` on reuse)."""
        slug = slugify(base)
        seen = self._counts.get(slug, 0) + 1
        self._counts[slug] = seen
        return slug if seen == 1 else f"{slug}-{seen}"
