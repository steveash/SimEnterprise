"""The :class:`LLMClient` — one interface over every backend (ARCHITECTURE.md §7/§16.4).

Callers (world-builders and producers) only ever see this client and the two
generation modes (§16.3). It wraps a :class:`Backend` with the cross-cutting
concerns the architecture demands:

* **Response cache** (D31) — checked first; a hit skips the backend entirely.
* **Retry with backoff** — retryable failures are retried up to a limit, honoring
  a provider ``Retry-After`` when given.
* **Cost accounting + ceiling** (D13) — every non-cached call's tokens are priced
  and aggregated; a call that would breach the ceiling raises *before* it runs.
* **Bounded concurrency** — :meth:`generate_many` fans out through a thread pool
  capped at ``max_concurrency`` (the Layer C parallelism dial, §16.4).

Determinism is structural (§7): the client decides *which* calls happen, in what
order, with what context — content varies, but the fake backend + caches make
re-runs reproducible.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from enterprise_sim.core.llm.backends import Backend, build_backend, estimate_tokens
from enterprise_sim.core.llm.cache import ResponseCache, request_key
from enterprise_sim.core.llm.pricing import DEFAULT_MODEL, ModelPricing, cost_of, estimate_cost
from enterprise_sim.core.llm.prompt import Prompt
from enterprise_sim.core.llm.types import (
    Completion,
    ContentResult,
    CostCeilingExceeded,
    LLMError,
    StructuredResult,
    TokenUsage,
    TransientLLMError,
    verify_references,
)


@dataclass(slots=True)
class CostTracker:
    """Running per-run cost accounting (§16.4).

    Aggregates :class:`TokenUsage` overall and per model and prices it via the
    pricing table so the client can enforce a ceiling and report a run total.
    Cache hits are counted (calls/usage) but priced at $0 — they cost nothing.
    """

    pricing_table: dict[str, ModelPricing] | None = None
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    usage_by_model: dict[str, TokenUsage] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    calls: int = 0
    cache_hits: int = 0

    def record(self, usage: TokenUsage, model: str, *, cached: bool) -> None:
        """Record one completed call's usage (priced at $0 when ``cached``)."""
        self.calls += 1
        self.total_usage = self.total_usage + usage
        self.usage_by_model[model] = self.usage_by_model.get(model, TokenUsage()) + usage
        if cached:
            self.cache_hits += 1
        else:
            self.total_cost_usd += cost_of(usage, model, table=self.pricing_table)

    def projected_cost(self, usage: TokenUsage, model: str) -> float:
        """Total cost if ``usage`` for ``model`` were added now (for ceiling checks)."""
        return self.total_cost_usd + cost_of(usage, model, table=self.pricing_table)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Configuration for an :class:`LLMClient` (snapshotted per run, §7).

    Everything that selects a backend, model, cost policy, or cache location lives
    here so a run can record exactly how its content was produced.
    """

    backend: str = "fake"
    model: str = DEFAULT_MODEL
    structured_temperature: float = 0.0
    content_temperature: float = 0.3
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    max_concurrency: int = 8
    cost_ceiling_usd: float | None = None
    cache_dir: str | None = None
    cache_enabled: bool = True


class LLMClient:
    """Provider-agnostic LLM client with caching, retry, cost, and concurrency."""

    def __init__(
        self,
        backend: Backend,
        *,
        config: LLMConfig | None = None,
        cache: ResponseCache | None = None,
        cost_tracker: CostTracker | None = None,
        pricing_table: dict[str, ModelPricing] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._backend = backend
        self._config = config or LLMConfig(backend=backend.name)
        self._cache = cache or ResponseCache(
            self._config.cache_dir, enabled=self._config.cache_enabled
        )
        self._cost = cost_tracker or CostTracker(pricing_table=pricing_table)
        self._sleep = sleep

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: LLMConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        pricing_table: dict[str, ModelPricing] | None = None,
        **backend_kwargs: Any,
    ) -> LLMClient:
        """Build a client (and its backend) from an :class:`LLMConfig`."""
        backend = build_backend(config.backend, **backend_kwargs)
        cache = ResponseCache(config.cache_dir, enabled=config.cache_enabled)
        cost = CostTracker(pricing_table=pricing_table)
        return cls(
            backend,
            config=config,
            cache=cache,
            cost_tracker=cost,
            sleep=sleep,
        )

    # -- introspection --------------------------------------------------------

    @property
    def cost(self) -> CostTracker:
        """The run's :class:`CostTracker` (spend, usage, cache-hit counts)."""
        return self._cost

    @property
    def cache(self) -> ResponseCache:
        """The on-disk :class:`ResponseCache` backing this client."""
        return self._cache

    @property
    def config(self) -> LLMConfig:
        """The (frozen) configuration this client was built with."""
        return self._config

    # -- generation modes -----------------------------------------------------

    def generate_structured(
        self,
        prompt: Prompt,
        schema: Mapping[str, Any],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> StructuredResult:
        """Forced schema-shaped output for world-building / metadata (§16.3, D32)."""
        model = model or self._config.model
        temperature = self._config.structured_temperature if temperature is None else temperature
        key = request_key(
            prompt=prompt,
            model=model,
            mode="structured",
            schema=schema,
            temperature=temperature,
        )
        completion = self._call(
            key,
            lambda: self._backend.generate_structured(
                prompt, schema=schema, model=model, temperature=temperature
            ),
        )
        if completion.structured is None:
            raise LLMError("structured generation returned no structured payload")
        return StructuredResult(
            data=completion.structured,
            usage=completion.usage,
            model=completion.model or model,
            cache_hit=completion.cache_hit,
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str] = (),
        model: str | None = None,
        temperature: float | None = None,
    ) -> ContentResult:
        """Prose generation with verified citations (§16.3, D32).

        The model's claimed ``references_used`` are verified against
        ``candidate_references`` — hallucinated ids are dropped — so the caller
        can safely create ``references`` edges (D16) from the result.
        """
        model = model or self._config.model
        temperature = self._config.content_temperature if temperature is None else temperature
        candidates = tuple(candidate_references)
        key = request_key(
            prompt=prompt,
            model=model,
            mode="content",
            candidates=candidates,
            temperature=temperature,
        )
        completion = self._call(
            key,
            lambda: self._backend.generate_content(
                prompt,
                candidate_references=candidates,
                model=model,
                temperature=temperature,
            ),
        )
        verified = verify_references(completion.references_used, candidates)
        return ContentResult(
            content=completion.text,
            references_used=verified,
            usage=completion.usage,
            model=completion.model or model,
            cache_hit=completion.cache_hit,
        )

    # -- batch ----------------------------------------------------------------

    def generate_many(self, tasks: Sequence[Callable[[LLMClient], Any]]) -> list[Any]:
        """Run ``tasks`` through a thread pool bounded by ``max_concurrency`` (§16.4).

        Each task is called with this client. Order of results matches ``tasks``.
        This is the Layer C parallelism dial; the on-disk cache and cost tracker
        are shared and thread-safe enough for the last-writer-wins cache.
        """
        if not tasks:
            return []
        workers = max(1, self._config.max_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda task: task(self), tasks))

    # -- estimation -----------------------------------------------------------

    def dry_run_estimate(
        self,
        *,
        num_tasks: int,
        input_tokens_each: int,
        output_tokens_each: int,
        model: str | None = None,
        cached_input_tokens_each: int = 0,
    ) -> float:
        """Estimate USD spend for a batch *before* running it (D13).

        If a ceiling is configured and the estimate breaches it, raises
        :class:`CostCeilingExceeded` so a large run can be gated up front.
        """
        model = model or self._config.model
        projected = estimate_cost(
            num_tasks=num_tasks,
            input_tokens_each=input_tokens_each,
            output_tokens_each=output_tokens_each,
            cached_input_tokens_each=cached_input_tokens_each,
            model=model,
            table=self._cost.pricing_table,
        )
        ceiling = self._config.cost_ceiling_usd
        if ceiling is not None and projected > ceiling:
            raise CostCeilingExceeded(projected, ceiling)
        return projected

    @staticmethod
    def estimate_prompt_tokens(prompt: Prompt) -> int:
        """A rough input-token estimate for ``prompt`` (for dry-run inputs)."""
        return estimate_tokens(prompt.text)

    # -- internals ------------------------------------------------------------

    def _call(self, key: str, run: Callable[[], Completion]) -> Completion:
        """Cache → ceiling → retry pipeline shared by both generation modes."""
        cached = self._cache.get(key)
        if cached is not None:
            self._cost.record(cached.usage, cached.model, cached=True)
            return cached

        completion = self._with_retry(run)

        # Enforce the ceiling on *actual* usage before accepting the call.
        ceiling = self._config.cost_ceiling_usd
        if ceiling is not None:
            projected = self._cost.projected_cost(completion.usage, completion.model)
            if projected > ceiling:
                raise CostCeilingExceeded(projected, ceiling)

        self._cost.record(completion.usage, completion.model, cached=False)
        self._cache.put(key, completion)
        return completion

    def _with_retry(self, run: Callable[[], Completion]) -> Completion:
        """Invoke ``run``, retrying transient failures with bounded backoff."""
        attempt = 0
        while True:
            try:
                return run()
            except TransientLLMError as exc:
                if attempt >= self._config.max_retries:
                    raise
                delay = self._backoff_delay(attempt, exc.retry_after)
                attempt += 1
                self._sleep(delay)

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Compute the backoff delay, honoring ``Retry-After`` when present.

        Deterministic exponential backoff (no random jitter) keeps the *timing
        structure* reproducible, consistent with §7's "determinism is structural".
        """
        cap = self._config.backoff_max_seconds
        if retry_after is not None:
            return retry_after if retry_after < cap else cap
        delay = self._config.backoff_base_seconds * (2**attempt)
        return delay if delay < cap else cap


def build_client(
    config: LLMConfig | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    **backend_kwargs: Any,
) -> LLMClient:
    """Convenience constructor: default to a deterministic ``fake`` client.

    With no config this yields the network-free fake client the test kit uses.
    """
    config = config or LLMConfig()
    return LLMClient.from_config(config, sleep=sleep, **backend_kwargs)


__all__ = [
    "CostTracker",
    "LLMClient",
    "LLMConfig",
    "build_client",
]
