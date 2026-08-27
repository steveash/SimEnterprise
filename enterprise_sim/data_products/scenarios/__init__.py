"""Data-scenario plugins: registered causal-graph templates.

A data scenario is a plugin (mirroring the four core registries, §4): it
declares a ``name``, a human ``title``, a natural-language ``brief`` (the
guidance an LLM-mode authoring pass elaborates), a deterministic
``build_spec`` — the rich, hand-authored causal graph that keeps keyless runs
realistic — and a ``question_bank`` whose gap-carrying entries drive the
iterative analysis loop. Registering a new scenario never touches the engine.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from enterprise_sim.core.registry import Registry
from enterprise_sim.data_products.spec import QuestionSpec, ScenarioSpec

__all__ = ["DATA_SCENARIOS", "DataScenario", "discover_scenarios"]


@runtime_checkable
class DataScenario(Protocol):
    """The plugin contract for one data scenario."""

    name: str
    title: str
    brief: str

    def build_spec(self, *, start: date, end: date) -> ScenarioSpec:
        """The deterministic template spec over the given window."""
        ...

    def question_bank(self, spec: ScenarioSpec) -> tuple[QuestionSpec, ...]:
        """Business questions (some with gap fixes) tailored to ``spec``."""
        ...


DATA_SCENARIOS: Registry[DataScenario] = Registry("data scenario")

_DISCOVERED = False


def discover_scenarios() -> Registry[DataScenario]:
    """Import the built-in scenario modules so their plugins register."""
    global _DISCOVERED
    if not _DISCOVERED:
        from enterprise_sim.data_products.scenarios import (  # noqa: F401
            marketing_attribution,
            product_growth,
            sales_pipeline,
            subscription_finance,
            support_ops,
        )

        _DISCOVERED = True
    return DATA_SCENARIOS
