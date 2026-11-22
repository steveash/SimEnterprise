"""The ``pipeline_ops`` data scenario template lints clean and ships a gap question."""

from __future__ import annotations

from datetime import date

from enterprise_sim.data_products.lint import require_clean
from enterprise_sim.data_products.scenarios import discover_scenarios


def test_pipeline_ops_data_scenario_lints_clean() -> None:
    spec = (
        discover_scenarios()
        .get("pipeline_ops")
        .build_spec(start=date(2026, 1, 5), end=date(2026, 3, 27))
    )
    require_clean(spec)
    assert spec.questions
    assert any(q.gap is not None for q in spec.questions)
