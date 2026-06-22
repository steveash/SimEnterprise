"""Model pricing table and cost arithmetic (ARCHITECTURE.md §16.4, D13).

Cost accounting turns :class:`TokenUsage` into dollars via a per-model price
table so the client can (a) aggregate per-run spend, (b) enforce a ceiling, and
(c) emit a dry-run estimate before a big render. Prices are USD per *million*
tokens and are intentionally easy to override — a run snapshots its config, so a
stale table never silently changes a past run's accounting.
"""

from __future__ import annotations

from dataclasses import dataclass

from enterprise_sim.core.llm.types import TokenUsage

# Default model used when a call does not name one.
DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per *million* tokens for one model.

    ``cached_input_per_mtok`` is the (cheaper) rate for prompt-cache reads; the
    layered prompt assembly (§16.1) exists precisely to push tokens onto this
    line. Unknown models fall back to :data:`FALLBACK_PRICING`.
    """

    input_per_mtok: float
    cached_input_per_mtok: float
    output_per_mtok: float

    def cost(self, usage: TokenUsage) -> float:
        """Return the USD cost of ``usage`` at these rates."""
        million = 1_000_000.0
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.cached_input_tokens * self.cached_input_per_mtok
            + usage.output_tokens * self.output_per_mtok
        ) / million


# Approximate published list prices (USD / Mtok). Override via config for a run.
PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-8": ModelPricing(15.0, 1.5, 75.0),
    "claude-sonnet-4-6": ModelPricing(3.0, 0.3, 15.0),
    "claude-haiku-4-5": ModelPricing(0.80, 0.08, 4.0),
}

# Used for any model not in :data:`PRICING` so accounting never silently zeroes.
FALLBACK_PRICING = ModelPricing(3.0, 0.3, 15.0)


def pricing_for(model: str, *, table: dict[str, ModelPricing] | None = None) -> ModelPricing:
    """Return the :class:`ModelPricing` for ``model``, falling back if unknown."""
    return (table or PRICING).get(model, FALLBACK_PRICING)


def cost_of(
    usage: TokenUsage, model: str, *, table: dict[str, ModelPricing] | None = None
) -> float:
    """Return the USD cost of ``usage`` for ``model``."""
    return pricing_for(model, table=table).cost(usage)


def estimate_cost(
    *,
    num_tasks: int,
    input_tokens_each: int,
    output_tokens_each: int,
    model: str,
    cached_input_tokens_each: int = 0,
    table: dict[str, ModelPricing] | None = None,
) -> float:
    """Dry-run estimate: ``num_tasks`` × per-task token estimate → USD (D13).

    Used to gate a large render before any call is made — multiply the artifact
    count by a per-artifact token estimate and compare against the ceiling.
    """
    per_task = TokenUsage(
        input_tokens=input_tokens_each,
        cached_input_tokens=cached_input_tokens_each,
        output_tokens=output_tokens_each,
    )
    return cost_of(per_task, model, table=table) * num_tasks
