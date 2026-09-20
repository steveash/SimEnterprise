"""Asserts ``tests/fixtures/evals_score_parity.json`` matches the live scorer.

The fixture (generated from :func:`enterprise_sim.benchmark.score.score_item`)
is what a TypeScript port of the scoring math is tested against; this pins it
to the real implementation so a scoring change without regenerating the
fixture fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

from enterprise_sim.benchmark.schema import QAPair
from enterprise_sim.benchmark.score import score_item

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "evals_score_parity.json"


def test_fixture_exists_and_matches_live_implementation() -> None:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 5

    for case in cases:
        pair = QAPair(
            id="qa-x",
            question="q",
            qtype="what",
            reasoning_type="direct_relation",
            expected_ids=tuple(case["expected_ids"]),
        )
        item = score_item(pair, frozenset(case["predicted_ids"]))
        assert item.precision == case["precision"], case["name"]
        assert item.recall == case["recall"], case["name"]
        assert item.f1 == case["f1"], case["name"]
        assert item.exact_match == case["exact_match"], case["name"]
