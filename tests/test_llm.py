"""Tests for the LLM provider abstraction & prompt assembly (esim-76f1003c).

Acceptance (ARCHITECTURE.md §7/§16, D29/D30/D31/D32):
* fake backend is deterministic — same prompt → byte-identical result, no network;
* the structured-output path returns schema-shaped data;
* cost accounting aggregates priced usage and the response cache produces hits.

NO real API calls happen here: every test runs on the ``fake`` backend or a tiny
in-test stub backend, so the suite is free, fast, and deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from enterprise_sim.core.llm import (
    BedrockBackend,
    Completion,
    CostCeilingExceeded,
    FakeBackend,
    LLMClient,
    LLMConfig,
    ModelPricing,
    Prompt,
    TokenUsage,
    TransientLLMError,
    assemble_prompt,
    build_backend,
    build_client,
    cost_of,
    estimate_cost,
    normalize_model_id,
    request_key,
    verify_references,
)
from enterprise_sim.core.llm.prompt import MAX_CACHE_BREAKPOINTS, PromptLayer

# ---------------------------------------------------------------------------
# Prompt assembly (§16.1, D29)
# ---------------------------------------------------------------------------


def _prompt(brief: str = "Write the Q2 status.") -> Prompt:
    return assemble_prompt(
        system="You write status reports.",
        stable_context=["ACME Corp profile", "Payments project context"],
        brief=brief,
        labels=["company_profile", "project_context"],
    )


def test_assemble_orders_stable_then_volatile() -> None:
    prompt = _prompt()
    roles = [layer.role for layer in prompt.layers]
    # All system (cacheable prefix) layers come before the single user (volatile) layer.
    assert roles == ["system", "system", "system", "user"]
    assert prompt.user_layers[-1].cacheable is False
    assert all(layer.cacheable for layer in prompt.system_layers)


def test_assemble_labels_stable_blocks() -> None:
    prompt = _prompt()
    labels = [layer.label for layer in prompt.system_layers]
    assert labels == ["system", "company_profile", "project_context"]


def test_assemble_rejects_too_many_cache_breakpoints() -> None:
    too_many = ["a", "b", "c", "d"]  # +system = 5 cacheable layers > 4
    with pytest.raises(ValueError, match="cache breakpoints"):
        assemble_prompt(system="s", stable_context=too_many, brief="b")


def test_assemble_allows_exactly_max_breakpoints() -> None:
    context = ["a", "b", "c"]  # +system = 4 == MAX
    prompt = assemble_prompt(system="s", stable_context=context, brief="b")
    assert len(prompt.cacheable_layers) == MAX_CACHE_BREAKPOINTS


def test_prompt_hash_is_stable_and_sensitive() -> None:
    a = _prompt("brief one")
    b = _prompt("brief one")
    c = _prompt("brief two")
    assert a.hash() == b.hash()  # identical prompts collide (the cross-artifact case)
    assert a.hash() != c.hash()  # a changed volatile brief changes the hash


def test_prompt_text_flattens_all_layers() -> None:
    prompt = Prompt(
        layers=(
            PromptLayer(role="system", text="S", cacheable=True),
            PromptLayer(role="user", text="U"),
        )
    )
    assert prompt.system_text == "S"
    assert prompt.user_text == "U"
    assert "S" in prompt.text and "U" in prompt.text


# ---------------------------------------------------------------------------
# Fake backend determinism (D31)
# ---------------------------------------------------------------------------


def test_fake_structured_is_deterministic() -> None:
    backend = FakeBackend()
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "active": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "role": {"type": "string", "enum": ["author", "reviewer"]},
        },
    }
    prompt = _prompt()
    first = backend.generate_structured(prompt, schema=schema, model="m", temperature=0.0)
    second = backend.generate_structured(prompt, schema=schema, model="m", temperature=0.0)
    assert first == second  # byte-identical, no network


def test_fake_structured_conforms_to_schema() -> None:
    backend = FakeBackend()
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "done": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": ["x", "y"]},
        },
    }
    result = backend.generate_structured(_prompt(), schema=schema, model="m", temperature=0.0)
    data = result.structured
    assert data is not None
    assert isinstance(data["title"], str)
    assert isinstance(data["count"], int)
    assert isinstance(data["ratio"], float)
    assert isinstance(data["done"], bool)
    assert isinstance(data["items"], list) and isinstance(data["items"][0], str)
    assert data["kind"] in ("x", "y")


def test_fake_content_is_deterministic_and_echoes_brief() -> None:
    backend = FakeBackend()
    prompt = _prompt("Summarize the sprint.")
    a = backend.generate_content(prompt, candidate_references=[], model="m", temperature=0.3)
    b = backend.generate_content(prompt, candidate_references=[], model="m", temperature=0.3)
    assert a == b
    assert "Summarize the sprint." in a.text


def test_fake_content_cites_only_candidates() -> None:
    backend = FakeBackend()
    candidates = ["art:1", "art:2", "art:3", "art:4"]
    result = backend.generate_content(
        _prompt(), candidate_references=candidates, model="m", temperature=0.3
    )
    assert set(result.references_used).issubset(set(candidates))


# ---------------------------------------------------------------------------
# Structured-output path through the client (§16.3)
# ---------------------------------------------------------------------------


def test_client_generate_structured_returns_data() -> None:
    client = build_client(LLMConfig(backend="fake"))
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    result = client.generate_structured(_prompt(), schema)
    assert "summary" in result.data
    assert result.cache_hit is False
    assert result.usage.total_tokens > 0


def test_client_generate_content_verifies_references() -> None:
    client = build_client(LLMConfig(backend="fake"))
    candidates = ["art:a", "art:b"]
    result = client.generate_content(_prompt(), candidate_references=candidates)
    assert set(result.references_used).issubset(set(candidates))


def test_verify_references_drops_hallucinations_and_dedupes() -> None:
    kept = verify_references(["art:a", "ghost", "art:a", "art:b"], ["art:a", "art:b"])
    assert kept == ("art:a", "art:b")


class _HallucinatingBackend:
    """A stub that claims a reference outside the candidate set (to test verification)."""

    name = "hallucinator"

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        raise NotImplementedError

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        return Completion(
            text="prose",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            model=model,
            references_used=("art:real", "art:ghost"),
        )


def test_client_drops_hallucinated_reference() -> None:
    client = LLMClient(_HallucinatingBackend(), config=LLMConfig(backend="hallucinator"))
    result = client.generate_content(_prompt(), candidate_references=["art:real"])
    assert result.references_used == ("art:real",)


# ---------------------------------------------------------------------------
# Cost accounting (§16.4, D13)
# ---------------------------------------------------------------------------


def test_cost_of_uses_split_rates() -> None:
    pricing = ModelPricing(input_per_mtok=3.0, cached_input_per_mtok=0.3, output_per_mtok=15.0)
    usage = TokenUsage(
        input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost_of(usage, "m", table={"m": pricing}) == pytest.approx(3.0 + 0.3 + 15.0)


def test_unknown_model_uses_fallback_pricing() -> None:
    usage = TokenUsage(input_tokens=1_000_000)
    # Fallback is non-zero, so accounting never silently zeroes an unknown model.
    assert cost_of(usage, "no-such-model") > 0


@pytest.mark.parametrize(
    ("bedrock_id", "expected"),
    [
        # Regional inference profiles (us./eu./apac.).
        ("us.anthropic.claude-sonnet-4-6-20250929-v1:0", "claude-sonnet-4-6"),
        ("eu.anthropic.claude-sonnet-4-6-20250929-v1:0", "claude-sonnet-4-6"),
        ("apac.anthropic.claude-sonnet-4-6-20250929-v1:0", "claude-sonnet-4-6"),
        # Bare (region-less) Bedrock id.
        ("anthropic.claude-opus-4-8-20250101-v1:0", "claude-opus-4-8"),
        # ARN ending in an inference-profile id.
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
            "us.anthropic.claude-sonnet-4-6-20250929-v1:0",
            "claude-sonnet-4-6",
        ),
        # A different dated variant / version bump still peels to the 1P name.
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", "claude-3-5-sonnet"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
    ],
)
def test_normalize_model_id_maps_bedrock_ids(bedrock_id: str, expected: str) -> None:
    assert normalize_model_id(bedrock_id) == expected


@pytest.mark.parametrize(
    "model",
    [
        # 1P ids pass through untouched...
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        # ...as do strings that don't match the Bedrock inference-profile shape.
        "no-such-model",
        "anthropic.claude-sonnet-4-6",  # no dated version suffix
        "gpt-4o",
    ],
)
def test_normalize_model_id_passes_through_non_bedrock(model: str) -> None:
    assert normalize_model_id(model) == model


def test_bedrock_id_prices_identically_to_1p_twin() -> None:
    # A Bedrock inference-profile id must cost exactly what its 1P twin costs
    # through the public cost path (D13 ceiling + dry-run estimates).
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=500_000)
    bedrock = cost_of(usage, "us.anthropic.claude-sonnet-4-6-20250929-v1:0")
    onep = cost_of(usage, "claude-sonnet-4-6")
    assert bedrock == pytest.approx(onep)
    # And it is *not* silently priced at the unknown-model fallback path.
    assert cost_of(usage, "claude-opus-4-8") != pytest.approx(onep)


def test_client_accumulates_cost() -> None:
    client = build_client(LLMConfig(backend="fake"))
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    client.generate_structured(_prompt("one"), schema)
    client.generate_structured(_prompt("two"), schema)
    assert client.cost.calls == 2
    assert client.cost.total_cost_usd > 0
    assert client.cost.total_usage.total_tokens > 0


def test_dry_run_estimate_scales_with_task_count() -> None:
    one = estimate_cost(
        num_tasks=1, input_tokens_each=1000, output_tokens_each=500, model="claude-sonnet-4-6"
    )
    hundred = estimate_cost(
        num_tasks=100, input_tokens_each=1000, output_tokens_each=500, model="claude-sonnet-4-6"
    )
    assert hundred == pytest.approx(one * 100)


def test_dry_run_estimate_raises_over_ceiling() -> None:
    client = build_client(LLMConfig(backend="fake", cost_ceiling_usd=0.001))
    with pytest.raises(CostCeilingExceeded):
        client.dry_run_estimate(
            num_tasks=10_000, input_tokens_each=10_000, output_tokens_each=5_000
        )


class _ExpensiveBackend:
    """A stub reporting huge token usage to trip the live cost ceiling."""

    name = "expensive"

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        return Completion(
            text="{}",
            usage=TokenUsage(input_tokens=10_000_000, output_tokens=10_000_000),
            model=model,
            structured={},
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        raise NotImplementedError


def test_live_call_enforces_cost_ceiling() -> None:
    client = LLMClient(
        _ExpensiveBackend(),
        config=LLMConfig(backend="expensive", cost_ceiling_usd=0.01),
    )
    with pytest.raises(CostCeilingExceeded):
        client.generate_structured(_prompt(), {"type": "object"})


# ---------------------------------------------------------------------------
# Response cache (D31)
# ---------------------------------------------------------------------------


def test_response_cache_hit_skips_backend(tmp_path: Any) -> None:
    backend = _CountingBackend()
    config = LLMConfig(backend="counter", cache_dir=str(tmp_path), cache_enabled=True)
    client = LLMClient(backend, config=config)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    first = client.generate_structured(_prompt(), schema)
    second = client.generate_structured(_prompt(), schema)

    assert backend.calls == 1  # second call served from disk
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.data == first.data
    assert client.cache.hits == 1


def test_cache_disabled_always_calls_backend(tmp_path: Any) -> None:
    backend = _CountingBackend()
    config = LLMConfig(backend="counter", cache_dir=str(tmp_path), cache_enabled=False)
    client = LLMClient(backend, config=config)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    client.generate_structured(_prompt(), schema)
    client.generate_structured(_prompt(), schema)
    assert backend.calls == 2


def test_cache_hit_costs_nothing_but_counts(tmp_path: Any) -> None:
    config = LLMConfig(backend="fake", cache_dir=str(tmp_path))
    client = build_client(config)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    client.generate_structured(_prompt(), schema)
    cost_after_first = client.cost.total_cost_usd
    client.generate_structured(_prompt(), schema)  # cache hit
    assert client.cost.total_cost_usd == cost_after_first  # hit priced at $0
    assert client.cost.cache_hits == 1
    assert client.cost.calls == 2


def test_request_key_separates_modes_and_models() -> None:
    prompt = _prompt()
    structured_key = request_key(
        prompt=prompt, model="m", mode="structured", schema={"type": "object"}
    )
    content_key = request_key(prompt=prompt, model="m", mode="content", candidates=("a",))
    other_model = request_key(
        prompt=prompt, model="n", mode="structured", schema={"type": "object"}
    )
    assert structured_key != content_key
    assert structured_key != other_model


class _CountingBackend:
    """A deterministic stub that counts how many times the backend actually ran."""

    name = "counter"

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        self.calls += 1
        return Completion(
            text='{"x": "v"}',
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model=model,
            structured={"x": "v"},
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        self.calls += 1
        return Completion(
            text="prose",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model=model,
        )


# ---------------------------------------------------------------------------
# Retry + backoff (§16.4)
# ---------------------------------------------------------------------------


class _FlakyBackend:
    """Fails with a transient error ``fail_times`` then succeeds."""

    name = "flaky"

    def __init__(self, fail_times: int, *, retry_after: float | None = None) -> None:
        self._fail_times = fail_times
        self._retry_after = retry_after
        self.attempts = 0

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise TransientLLMError("rate limited", retry_after=self._retry_after)
        return Completion(text="{}", usage=TokenUsage(input_tokens=1), model=model, structured={})

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        raise NotImplementedError


def test_retry_recovers_after_transient_failures() -> None:
    sleeps: list[float] = []
    backend = _FlakyBackend(fail_times=2)
    client = LLMClient(
        backend,
        config=LLMConfig(backend="flaky", max_retries=3, backoff_base_seconds=1.0),
        sleep=sleeps.append,
    )
    result = client.generate_structured(_prompt(), {"type": "object"})
    assert result.data == {}
    assert backend.attempts == 3
    assert sleeps == [1.0, 2.0]  # deterministic exponential backoff, no jitter


def test_retry_gives_up_after_max_retries() -> None:
    backend = _FlakyBackend(fail_times=99)
    client = LLMClient(
        backend,
        config=LLMConfig(backend="flaky", max_retries=2, backoff_base_seconds=0.0),
        sleep=lambda _: None,
    )
    with pytest.raises(TransientLLMError):
        client.generate_structured(_prompt(), {"type": "object"})
    assert backend.attempts == 3  # initial + 2 retries


def test_retry_honors_retry_after_header() -> None:
    sleeps: list[float] = []
    backend = _FlakyBackend(fail_times=1, retry_after=7.0)
    client = LLMClient(
        backend,
        config=LLMConfig(backend="flaky", max_retries=3, backoff_max_seconds=30.0),
        sleep=sleeps.append,
    )
    client.generate_structured(_prompt(), {"type": "object"})
    assert sleeps == [7.0]  # honored Retry-After instead of computed backoff


# ---------------------------------------------------------------------------
# Bounded concurrency (§16.4)
# ---------------------------------------------------------------------------


def test_generate_many_runs_all_tasks_in_order() -> None:
    client = build_client(LLMConfig(backend="fake", max_concurrency=4))
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    def task_for(i: int) -> Any:
        return lambda c: c.generate_structured(_prompt(f"brief {i}"), schema).data

    results = client.generate_many([task_for(i) for i in range(10)])
    assert len(results) == 10
    assert all(isinstance(r, dict) for r in results)


def test_generate_many_empty_is_noop() -> None:
    client = build_client(LLMConfig(backend="fake"))
    assert client.generate_many([]) == []


# ---------------------------------------------------------------------------
# Backend factory (§7)
# ---------------------------------------------------------------------------


def test_build_client_defaults_to_fake() -> None:
    client = build_client()
    assert client.config.backend == "fake"


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown LLM backend"):
        build_client(LLMConfig(backend="nope"))


# ---------------------------------------------------------------------------
# Bedrock region/profile config surface (spec 0001, slice 2)
# ---------------------------------------------------------------------------


def test_llm_config_defaults_leave_fake_backend_unchanged() -> None:
    # The new bedrock-only fields default to None and never touch other backends:
    # a default fake client builds and stores no region/profile.
    config = LLMConfig()
    assert config.aws_region is None
    assert config.aws_profile is None
    client = build_client(config)
    assert client.config.backend == "fake"


def test_bedrock_backend_stores_region_and_profile() -> None:
    # Kwargs are stored on the backend; SDK client creation stays lazy, so this
    # constructs without importing anthropic or touching AWS.
    backend = BedrockBackend(aws_region="eu-west-1", aws_profile="sim")
    assert backend._aws_region == "eu-west-1"
    assert backend._aws_profile == "sim"


def test_bedrock_backend_defaults_are_none() -> None:
    backend = BedrockBackend()
    assert backend._aws_region is None
    assert backend._aws_profile is None


def test_build_backend_threads_bedrock_region_and_profile() -> None:
    backend = build_backend("bedrock", aws_region="us-west-2", aws_profile="dev")
    assert isinstance(backend, BedrockBackend)
    assert backend._aws_region == "us-west-2"
    assert backend._aws_profile == "dev"


def test_bedrock_config_threads_region_and_profile_into_backend() -> None:
    # from_config projects the LLMConfig's bedrock fields onto the backend it builds.
    config = LLMConfig(backend="bedrock", aws_region="ap-southeast-2", aws_profile="prod")
    client = LLMClient.from_config(config)
    backend = client._backend
    assert isinstance(backend, BedrockBackend)
    assert backend._aws_region == "ap-southeast-2"
    assert backend._aws_profile == "prod"


def test_non_bedrock_backend_ignores_region_and_profile() -> None:
    # The region/profile are scoped to bedrock; a fake config carrying them (however
    # nonsensical) must not forward unexpected kwargs to a non-bedrock backend.
    config = LLMConfig(backend="fake", aws_region="us-east-1", aws_profile="x")
    client = LLMClient.from_config(config)
    assert isinstance(client._backend, FakeBackend)
