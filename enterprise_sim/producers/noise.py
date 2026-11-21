"""Messiness: surface noise in generated prose (``[messiness]``), every injection labelled.

Real corpora are not clean: people misspell words, call colleagues by nickname
or initials, write dates five different ways, and paste chatter into documents.
:class:`ProseNoise` applies those to every piece of prose a producer generated
(document bodies and review/Jira comments), whatever backend wrote it:

* ``typo_rate`` — per eligible word (lower-case, five letters or more, not a
  roster name): swap two inner letters, drop one, or double one;
* ``alias_rate`` — per person mention: first name only, ``F. Last``, initials,
  or a lower-case handle (``flast``) instead of the canonical name;
* ``date_format_mix`` — per ISO date: ``May 14, 2026``, ``14/05/2026``,
  ``05/14/26`` or ``14 May``;
* ``filler_rate`` — per text: an off-topic paragraph (chatter, boilerplate).

Each change is recorded as a **quirk** (``{"kind", "original", "surface"}``,
plus ``"entity"`` for an alias) under the key the producer passed — the
artifact id — and the corpus writes them to the artifact's gold node
(``props["quirks"]``). Draws come from a seed of ``(world seed, key)``, so the
same world is noised identically. A :class:`ProseNoise` belongs to one
scenario's render (one thread); its quirk log is not shared.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence
from datetime import date

from enterprise_sim.core.config.diversity import MessinessConfig

__all__ = ["ProseNoise", "is_clean"]

_WORD = re.compile(r"(?<![\w-])[a-z]{5,}(?![\w-])")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_FILLER: tuple[str, ...] = (
    "Reminder: the kitchen on the third floor is closed on Friday for cleaning.",
    "Sent from my phone, sorry for the typos.",
    "Lunch-and-learn next Thursday; bring questions.",
    "Please ignore the previous version of this, it went out by mistake.",
    "Out of office next week with limited access to email.",
    "Copying the notes from the call below, unedited.",
    "Note to self: double-check the numbers before sharing more widely.",
)


def is_clean(cfg: MessinessConfig) -> bool:
    """Whether ``cfg`` injects nothing (every rate zero)."""
    return not any(cfg.model_dump().values())


def _typo(rng: random.Random, word: str) -> str:
    inner = list(word)
    kind = rng.choice(("swap", "drop", "double"))
    i = rng.randint(1, len(inner) - 3)
    if kind == "swap":
        inner[i], inner[i + 1] = inner[i + 1], inner[i]
    elif kind == "drop":
        del inner[i]
    else:
        inner.insert(i, inner[i])
    out = "".join(inner)
    return out if out != word else word[:-1]


def _alias(rng: random.Random, name: str) -> str:
    parts = name.split()
    if len(parts) < 2:
        return name.lower()
    first, last = parts[0], parts[-1]
    return rng.choice(
        (
            first,
            f"{first[0]}. {last}",
            f"{first[0]}{last[0]}",
            f"{first[0].lower()}{last.lower()}",
        )
    )


def _date(rng: random.Random, iso: str) -> str:
    day = date.fromisoformat(iso)
    month = _MONTHS[day.month - 1]
    return rng.choice(
        (
            f"{month} {day.day}, {day.year}",
            f"{day.day:02d}/{day.month:02d}/{day.year}",
            f"{day.month:02d}/{day.day:02d}/{day.year % 100:02d}",
            f"{day.day} {month}",
        )
    )


class ProseNoise:
    """Applies ``[messiness]`` to prose and keeps the quirk log per key."""

    def __init__(self, cfg: MessinessConfig, *, seed: int) -> None:
        self._cfg = cfg
        self._seed = seed
        self._quirks: dict[str, list[dict[str, str]]] = {}

    @property
    def config(self) -> MessinessConfig:
        return self._cfg

    @property
    def seed(self) -> int:
        return self._seed

    def _rng(self, key: str) -> random.Random:
        digest = hashlib.sha256(f"noise\x00{self._seed}\x00{key}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    def quirks(self, key: str) -> list[dict[str, str]]:
        """Every quirk recorded under ``key`` (texts whose key starts with ``key#`` too)."""
        out: list[dict[str, str]] = []
        for k in sorted(self._quirks):
            if k == key or k.startswith(f"{key}#"):
                out.extend(self._quirks[k])
        return out

    def apply(self, text: str, *, key: str, people: Sequence[tuple[str, str]] = ()) -> str:
        """Noise ``text`` and record its quirks under ``key``.

        ``people`` is ``(entity id, canonical name)`` of the persons the text
        may mention (the producer's roster).
        """
        cfg = self._cfg
        rng = self._rng(key)
        quirks = self._quirks.setdefault(key, [])
        protected = {w.lower() for _, name in people for w in name.split()}
        if cfg.alias_rate > 0:
            for entity, name in sorted(people, key=lambda p: -len(p[1])):
                if name not in text:
                    continue

                def swap(match: re.Match[str], entity: str = entity, name: str = name) -> str:
                    if rng.random() >= cfg.alias_rate:
                        return match.group(0)
                    surface = _alias(rng, name)
                    quirks.append(
                        {"kind": "alias", "original": name, "surface": surface, "entity": entity}
                    )
                    return surface

                text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", swap, text)
        if cfg.date_format_mix > 0:

            def redate(match: re.Match[str]) -> str:
                iso = match.group(0)
                if rng.random() >= cfg.date_format_mix:
                    return iso
                try:
                    surface = _date(rng, iso)
                except ValueError:
                    return iso
                quirks.append({"kind": "date_format", "original": iso, "surface": surface})
                return surface

            text = _ISO.sub(redate, text)
        if cfg.typo_rate > 0:

            def misspell(match: re.Match[str]) -> str:
                word = match.group(0)
                if word in protected or rng.random() >= cfg.typo_rate:
                    return word
                surface = _typo(rng, word)
                quirks.append({"kind": "typo", "original": word, "surface": surface})
                return surface

            text = _WORD.sub(misspell, text)
        if cfg.filler_rate > 0 and rng.random() < cfg.filler_rate:
            filler = rng.choice(_FILLER)
            quirks.append({"kind": "filler", "original": "", "surface": filler})
            text = f"{text}\n\n{filler}" if "\n" in text else f"{text} {filler}"
        return text
