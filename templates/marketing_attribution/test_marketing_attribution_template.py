"""The ``marketing_attribution`` data scenario template lints clean and ships gap questions."""

from __future__ import annotations

from datetime import date

from enterprise_sim.data_products.lint import require_clean
from enterprise_sim.data_products.scenarios import discover_scenarios


def test_marketing_attribution_data_scenario_lints_clean() -> None:
    spec = (
        discover_scenarios()
        .get("marketing_attribution")
        .build_spec(start=date(2026, 1, 5), end=date(2026, 3, 1))
    )
    require_clean(spec)
    assert spec.questions
    assert any(q.gap is not None for q in spec.questions)
