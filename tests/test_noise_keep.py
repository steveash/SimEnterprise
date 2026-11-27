"""Prose noise keeps the values a document must state as written, and only those."""

from __future__ import annotations

from enterprise_sim.core.config.diversity import MessinessConfig
from enterprise_sim.facts.ledger import required_values
from enterprise_sim.producers.noise import ProseNoise

_TEXT = "The target date of Atlas is 2026-06-07. The review moved to 2026-06-08 for Ada Diaz."
_PEOPLE = [("person:ada-diaz", "Ada Diaz")]


def _noise() -> ProseNoise:
    return ProseNoise(MessinessConfig(date_format_mix=1.0, alias_rate=1.0), seed=3)


def test_a_kept_value_stays_and_everything_else_is_still_noised() -> None:
    out = _noise().apply(_TEXT, key="artifact:a", people=_PEOPLE, keep=["2026-06-07"])
    assert "2026-06-07" in out
    assert "2026-06-08" not in out  # not kept: the date rule still applies
    assert "Ada Diaz" not in out  # nor does keeping one value stop the alias rule


def test_without_keep_the_noise_is_unchanged() -> None:
    plain = _noise().apply(_TEXT, key="artifact:a", people=_PEOPLE)
    assert plain == _noise().apply(_TEXT, key="artifact:a", people=_PEOPLE, keep=())
    assert "2026-06-07" not in plain and "2026-06-08" not in plain


def test_a_kept_value_inside_a_longer_one_and_repeats() -> None:
    text = "Due 2026-06-07; again 2026-06-07, then 2026-06-07T10:00."
    out = _noise().apply(text, key="artifact:b", keep=["2026-06-07", ""])
    assert out.count("2026-06-07") == 3


def test_required_values_are_the_required_facts_values_only() -> None:
    payload = {
        "required_facts": ["fact:a"],
        "facts": [
            {"fact": "fact:a", "value": "2026-06-07"},
            {"fact": "fact:b", "value": "2026-07-01"},
        ],
    }
    assert required_values(payload) == ("2026-06-07",)
    assert required_values({"facts": payload["facts"]}) == ()
