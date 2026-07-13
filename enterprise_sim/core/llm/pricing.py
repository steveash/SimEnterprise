"""Model pricing table and cost arithmetic (ARCHITECTURE.md §16.4, D13).

Cost accounting turns :class:`TokenUsage` into dollars via a per-model price
table so the client can (a) aggregate per-run spend, (b) enforce a ceiling, and
(c) emit a dry-run estimate before a big render. Prices are USD per *million*
tokens and are intentionally easy to override — a run snapshots its config, so a
stale table never silently changes a past run's accounting.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from enterprise_sim.core.llm.types import TokenUsage

# Default model used when a call does not name one.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Bedrock exposes Anthropic models under inference-profile ids that wrap the 1P
# model name in a region prefix ("us."/"eu."/"apac.") and a "-YYYYMMDD-vN:N"
# release/version suffix, e.g. ``us.anthropic.claude-sonnet-4-6-20250929-v1:0``;
# ARNs end in that same id. Cost accounting keys on the 1P name, so we peel the
# id back to it. The model group is non-greedy so it stops at the dated suffix
# rather than swallowing the ``20250929`` digits, and the ``$`` anchor lets the
# match cover a bare id, a regional profile, or a trailing-id ARN alike.
_BEDROCK_MODEL_RE = re.compile(r"anthropic\.(claude-[a-z0-9-]+?)-\d{8}-v\d+:\d+$")


def normalize_model_id(model: str) -> str:
    """Map a Bedrock inference-profile/ARN model id to its 1P pricing key.

    ``us.anthropic.claude-sonnet-4-6-20250929-v1:0`` (or the ARN ending in one)
    prices identically to the 1P ``claude-sonnet-4-6``; this strips the region
    prefix and dated version suffix so :func:`pricing_for` finds the right row.
    A 1P id and any string that does not match the Bedrock shape pass through
    unchanged, so a genuinely unknown model still falls back exactly as before.
    """
    match = _BEDROCK_MODEL_RE.search(model)
    return match.group(1) if match else model


def looks_like_bedrock_model_id(model: str) -> bool:
    """Return whether ``model`` is a Bedrock inference-profile / ARN model id (finding F2).

    True for the ``…anthropic.claude-<family>-<YYYYMMDD>-vN:N`` shape (bare,
    region-prefixed, or as an ARN suffix) that the Bedrock backend must be given —
    the same family :func:`normalize_model_id` recognises, so the two never drift. A
    1P id (``claude-sonnet-4-6``) or any other string returns ``False``, which lets a
    client build reject it *before* the first live call rather than letting Bedrock
    fail on the 1P id (we deliberately don't map 1P→Bedrock: dated inference-profile
    ids can't be verified offline, so a wrong table would be worse than none).
    """
    return _BEDROCK_MODEL_RE.search(model) is not None


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

# Normalized model keys already warned about (finding F5): a fallback is emitted
# once per model, not once per call, so a long render doesn't spam stderr.
_FALLBACK_WARNED: set[str] = set()


def _warn_pricing_fallback(model: str, key: str) -> None:
    """Warn (once per model) that ``model`` is priced at fallback rates (finding F5).

    An unknown model — a fresh 1P id, or a Bedrock id whose family isn't in
    :data:`PRICING` (e.g. opus behind a custom application-inference-profile ARN) —
    is billed at :data:`FALLBACK_PRICING` (the sonnet rate). That *under*-prices a
    pricier model, so the D13 cost ceiling and dry-run estimate silently under-
    enforce. This surfaces the degradation deterministically without failing the
    run; the dedup keeps it to one line per distinct model.
    """
    if key in _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED.add(key)
    warnings.warn(
        f"model {model!r} has no pricing entry (keyed as {key!r}); billing at fallback "
        f"rates (${FALLBACK_PRICING.input_per_mtok:g}/${FALLBACK_PRICING.output_per_mtok:g} "
        f"per Mtok in/out) — the D13 cost ceiling may under-enforce for this model",
        stacklevel=3,
    )


def pricing_for(model: str, *, table: dict[str, ModelPricing] | None = None) -> ModelPricing:
    """Return the :class:`ModelPricing` for ``model``, falling back if unknown.

    ``model`` is normalized first (:func:`normalize_model_id`) so a Bedrock
    inference-profile id prices identically to its 1P equivalent (D13). A model that
    resolves to no pricing row falls back to :data:`FALLBACK_PRICING` and warns once
    (finding F5) so the cost ceiling's under-enforcement is visible, not silent.
    """
    key = normalize_model_id(model)
    resolved = (table or PRICING).get(key)
    if resolved is not None:
        return resolved
    _warn_pricing_fallback(model, key)
    return FALLBACK_PRICING


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
