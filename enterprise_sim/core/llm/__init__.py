"""LLM provider abstraction & prompt assembly (ARCHITECTURE.md §7/§16, esim-76f1003c).

A single :class:`LLMClient` over four config-selected backends — ``anthropic_api``,
``bedrock`` (same SDK), ``claude_cli``, and a deterministic ``fake``/echo — with
layered cache-aware prompt assembly (D29), grounded reference capture (D32),
retry+backoff, bounded concurrency, cost accounting + ceiling + dry-run estimate
(D13), and an on-disk response cache keyed by ``(prompt_hash, model)`` (D31).

Typical use::

    from enterprise_sim.core.llm import LLMConfig, assemble_prompt, build_client

    client = build_client(LLMConfig(backend="fake"))
    prompt = assemble_prompt(
        system="You write weekly status reports.",
        stable_context=[company_profile, project_context],
        brief=task_brief_with_roster,
    )
    result = client.generate_content(prompt, candidate_references=recent_artifact_ids)
"""

from __future__ import annotations

from enterprise_sim.core.llm.backends import (
    AnthropicAPIBackend,
    Backend,
    BedrockBackend,
    ClaudeCLIBackend,
    FakeBackend,
    build_backend,
    estimate_tokens,
)
from enterprise_sim.core.llm.cache import ResponseCache, request_key
from enterprise_sim.core.llm.client import (
    CostTracker,
    LLMClient,
    LLMConfig,
    build_client,
)
from enterprise_sim.core.llm.pricing import (
    DEFAULT_MODEL,
    PRICING,
    ModelPricing,
    cost_of,
    estimate_cost,
    normalize_model_id,
    pricing_for,
)
from enterprise_sim.core.llm.prompt import (
    MAX_CACHE_BREAKPOINTS,
    Prompt,
    PromptLayer,
    assemble_prompt,
)
from enterprise_sim.core.llm.types import (
    Completion,
    ContentResult,
    CostCeilingExceeded,
    LLMError,
    ReferenceVerificationError,
    StructuredResult,
    TokenUsage,
    TransientLLMError,
    verify_references,
)

__all__ = [
    "DEFAULT_MODEL",
    "MAX_CACHE_BREAKPOINTS",
    "PRICING",
    "AnthropicAPIBackend",
    "Backend",
    "BedrockBackend",
    "ClaudeCLIBackend",
    "Completion",
    "ContentResult",
    "CostCeilingExceeded",
    "CostTracker",
    "FakeBackend",
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "ModelPricing",
    "Prompt",
    "PromptLayer",
    "ReferenceVerificationError",
    "ResponseCache",
    "StructuredResult",
    "TokenUsage",
    "TransientLLMError",
    "assemble_prompt",
    "build_backend",
    "build_client",
    "cost_of",
    "estimate_cost",
    "estimate_tokens",
    "normalize_model_id",
    "pricing_for",
    "request_key",
    "verify_references",
]
