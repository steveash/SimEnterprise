"""Keyless tests for the shared Anthropic-SDK request path (spec 0001, slice 5).

Both :class:`AnthropicAPIBackend` and :class:`BedrockBackend` inherit their request
path from :class:`_AnthropicSDKBackend` (§7). The SDK client is duck-typed ``Any``,
so a tiny stub exposing ``messages.create`` exercises the whole path with **no SDK
import and no network**. These tests pin the request shape (cache-control layering,
forced tool-use, schema/model/temperature passthrough), the two completion parsers,
usage mapping, and error normalization — the surface that used to be uniformly
``pragma: no cover``. Both SDK backends are parametrized to prove they share it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from enterprise_sim.core.llm.backends import (
    AnthropicAPIBackend,
    BedrockBackend,
    _AnthropicSDKBackend,
    _normalize_sdk_error,
    _retry_after_from,
    _usage_from_sdk,
)
from enterprise_sim.core.llm.prompt import Prompt, PromptLayer
from enterprise_sim.core.llm.types import LLMError, TokenUsage, TransientLLMError

# ---------------------------------------------------------------------------
# Stubs: a duck-typed stand-in for the official SDK client and its responses.
# ---------------------------------------------------------------------------


class _StubMessages:
    """Records every ``create`` kwargs dict and returns a canned response (or raises)."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _StubClient:
    """Stand-in for ``anthropic.Anthropic`` / ``AnthropicBedrock`` (duck-typed ``Any``)."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.messages = _StubMessages(response, error)


class _StubSDKError(Exception):
    """A stand-in for an ``anthropic`` SDK exception, shaped how the normalizer reads it.

    ``_normalize_sdk_error`` only ``getattr``s ``status_code`` and ``response.headers``,
    so this reproduces exactly that surface without importing the SDK's error types.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers) if headers is not None else None


class _StubSDKBackend(_AnthropicSDKBackend):
    """SDK backend whose ``_make_client`` returns a stub — exercises lazy creation."""

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stub = client
        self.make_client_calls = 0

    def _make_client(self) -> Any:
        self.make_client_calls += 1
        return self._stub


# The two production backends that share the request path.
_SDK_BACKENDS = [AnthropicAPIBackend, BedrockBackend]


def _backend_with_client(
    cls: type[_AnthropicSDKBackend], client: Any, *, max_tokens: int = 4096
) -> _AnthropicSDKBackend:
    """Construct ``cls`` and inject a stub client via the ``_client`` cache slot."""
    backend = cls(max_tokens=max_tokens)
    backend._client = client
    return backend


def _tool_use_response(tool_input: dict[str, Any], usage: Any = None) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", input=tool_input)
    return SimpleNamespace(content=[block], usage=usage if usage is not None else SimpleNamespace())


def _simple_prompt() -> Prompt:
    return Prompt(
        layers=(
            PromptLayer(role="system", text="S", cacheable=True, label="system"),
            PromptLayer(role="user", text="U"),
        )
    )


def _mixed_prompt() -> Prompt:
    """A prompt with one cacheable and one non-cacheable system layer + a user layer."""
    return Prompt(
        layers=(
            PromptLayer(role="system", text="SYS", cacheable=True, label="system"),
            PromptLayer(role="system", text="CTX", cacheable=False, label="uncacheable"),
            PromptLayer(role="user", text="do the thing"),
        )
    )


# ---------------------------------------------------------------------------
# _call_tool: request shape (§16.1 cache layering, §16.3 forced tool-use)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_call_tool_builds_expected_request(cls: type[_AnthropicSDKBackend]) -> None:
    usage = SimpleNamespace(input_tokens=7, cache_read_input_tokens=3, output_tokens=5)
    client = _StubClient(_tool_use_response({"answer": 42}, usage))
    backend = _backend_with_client(cls, client, max_tokens=1234)
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}

    data, mapped_usage = backend._call_tool(
        _mixed_prompt(),
        schema=schema,
        model="claude-sonnet-4-6",
        temperature=0.2,
        tool_name="my_tool",
    )

    # Extracted tool_use input and mapped usage flow straight through.
    assert data == {"answer": 42}
    assert mapped_usage == TokenUsage(input_tokens=7, cached_input_tokens=3, output_tokens=5)

    (call,) = client.messages.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 1234
    assert call["temperature"] == 0.2
    # cache_control appears only on the cacheable layer.
    assert call["system"] == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "CTX"},
    ]
    # Forced tool-use carrying the caller's schema and tool name.
    assert call["tool_choice"] == {"type": "tool", "name": "my_tool"}
    assert call["tools"][0]["name"] == "my_tool"
    assert call["tools"][0]["input_schema"] == schema
    # The volatile user text becomes the single user message.
    assert call["messages"] == [{"role": "user", "content": "do the thing"}]


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_call_tool_raises_without_tool_use_block(cls: type[_AnthropicSDKBackend]) -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="just prose")],
        usage=SimpleNamespace(),
    )
    backend = _backend_with_client(cls, _StubClient(response))
    with pytest.raises(LLMError, match="no tool_use block"):
        backend._call_tool(
            _simple_prompt(),
            schema={"type": "object"},
            model="m",
            temperature=0.0,
            tool_name="t",
        )


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_call_tool_normalizes_sdk_exception(cls: type[_AnthropicSDKBackend]) -> None:
    # An error raised by ``messages.create`` is normalized into our hierarchy.
    client = _StubClient(error=_StubSDKError("rate limited", status_code=429))
    backend = _backend_with_client(cls, client)
    with pytest.raises(TransientLLMError):
        backend._call_tool(
            _simple_prompt(),
            schema={"type": "object"},
            model="m",
            temperature=0.0,
            tool_name="t",
        )


def test_client_created_lazily_and_cached() -> None:
    # The lazy-creation path (`_make_client` via `_client_or_create`): built once,
    # then reused across calls — proven with a subclass overriding `_make_client`.
    client = _StubClient(_tool_use_response({"ok": True}))
    backend = _StubSDKBackend(client)
    assert backend.make_client_calls == 0  # nothing constructed at __init__

    for _ in range(2):
        backend._call_tool(
            _simple_prompt(),
            schema={"type": "object"},
            model="m",
            temperature=0.0,
            tool_name="t",
        )
    assert backend.make_client_calls == 1  # created once, then cached


# ---------------------------------------------------------------------------
# generate_structured / generate_content: envelope parsing (§16.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_generate_structured_returns_sorted_json(cls: type[_AnthropicSDKBackend]) -> None:
    client = _StubClient(_tool_use_response({"b": 2, "a": 1}))
    backend = _backend_with_client(cls, client)

    completion = backend.generate_structured(
        _simple_prompt(), schema={"type": "object"}, model="m", temperature=0.0
    )

    assert completion.structured == {"b": 2, "a": 1}
    assert completion.text == '{"a": 1, "b": 2}'  # sorted-key JSON text
    assert completion.model == "m"
    assert client.messages.calls[0]["tool_choice"]["name"] == "emit_structured_output"


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_generate_content_parses_envelope(cls: type[_AnthropicSDKBackend]) -> None:
    client = _StubClient(_tool_use_response({"content": "hi", "references_used": ["x", "y"]}))
    backend = _backend_with_client(cls, client)

    completion = backend.generate_content(
        _simple_prompt(), candidate_references=["x"], model="m", temperature=0.5
    )

    assert completion.text == "hi"
    assert completion.references_used == ("x", "y")
    assert completion.structured == {"content": "hi", "references_used": ["x", "y"]}
    call = client.messages.calls[0]
    assert call["tool_choice"]["name"] == "emit_content"
    # The envelope schema (not the caller's) is forwarded as the tool input_schema.
    assert "content" in call["tools"][0]["input_schema"]["properties"]


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_generate_content_tolerates_missing_references(cls: type[_AnthropicSDKBackend]) -> None:
    client = _StubClient(_tool_use_response({"content": "solo"}))
    backend = _backend_with_client(cls, client)

    completion = backend.generate_content(
        _simple_prompt(), candidate_references=[], model="m", temperature=0.5
    )

    assert completion.text == "solo"
    assert completion.references_used == ()


@pytest.mark.parametrize("cls", _SDK_BACKENDS)
def test_generate_content_tolerates_missing_content(cls: type[_AnthropicSDKBackend]) -> None:
    client = _StubClient(_tool_use_response({"references_used": ["x"]}))
    backend = _backend_with_client(cls, client)

    completion = backend.generate_content(
        _simple_prompt(), candidate_references=["x"], model="m", temperature=0.5
    )

    assert completion.text == ""  # missing content defaults to empty prose
    assert completion.references_used == ("x",)


# ---------------------------------------------------------------------------
# _usage_from_sdk: split accounting + version-drift tolerance (§16.4)
# ---------------------------------------------------------------------------


def test_usage_from_sdk_maps_and_splits() -> None:
    raw = SimpleNamespace(input_tokens=100, cache_read_input_tokens=40, output_tokens=25)
    usage = _usage_from_sdk(raw)
    assert usage.input_tokens == 100
    assert usage.cached_input_tokens == 40  # kept split, not folded into input
    assert usage.output_tokens == 25


def test_usage_from_sdk_tolerates_missing_fields() -> None:
    # An object lacking the fields entirely → all zeros (SDK-version drift).
    assert _usage_from_sdk(SimpleNamespace()) == TokenUsage()


def test_usage_from_sdk_tolerates_none_fields() -> None:
    # Explicit ``None`` values coalesce to zero rather than raising.
    raw = SimpleNamespace(input_tokens=None, cache_read_input_tokens=None, output_tokens=None)
    assert _usage_from_sdk(raw) == TokenUsage()


# ---------------------------------------------------------------------------
# _normalize_sdk_error / _retry_after_from: transient vs terminal (§16.4)
# ---------------------------------------------------------------------------


def test_normalize_rate_limit_is_transient() -> None:
    result = _normalize_sdk_error(_StubSDKError("rate limited", status_code=429))
    assert isinstance(result, TransientLLMError)


@pytest.mark.parametrize("status", [500, 503, 599])
def test_normalize_server_error_is_transient(status: int) -> None:
    result = _normalize_sdk_error(_StubSDKError("boom", status_code=status))
    assert isinstance(result, TransientLLMError)


@pytest.mark.parametrize("status", [400, 401, 404, 600])
def test_normalize_other_status_is_terminal(status: int) -> None:
    result = _normalize_sdk_error(_StubSDKError("bad request", status_code=status))
    assert isinstance(result, LLMError)
    assert not isinstance(result, TransientLLMError)


def test_normalize_no_status_is_terminal() -> None:
    result = _normalize_sdk_error(_StubSDKError("something odd"))
    assert isinstance(result, LLMError)
    assert not isinstance(result, TransientLLMError)


def test_normalize_extracts_numeric_retry_after() -> None:
    result = _normalize_sdk_error(
        _StubSDKError("rate limited", status_code=429, headers={"retry-after": "12"})
    )
    assert isinstance(result, TransientLLMError)
    assert result.retry_after == 12.0


def test_normalize_garbage_retry_after_still_transient_for_429() -> None:
    # A 429 stays transient even if its Retry-After header can't be parsed.
    result = _normalize_sdk_error(
        _StubSDKError("rate limited", status_code=429, headers={"retry-after": "soon"})
    )
    assert isinstance(result, TransientLLMError)
    assert result.retry_after is None


def test_normalize_retry_after_without_status_is_transient() -> None:
    # A parseable Retry-After alone (no 429/5xx) is still treated as retryable;
    # the capitalized header name exercises the case-insensitive fallback.
    result = _normalize_sdk_error(_StubSDKError("throttled", headers={"Retry-After": "5"}))
    assert isinstance(result, TransientLLMError)
    assert result.retry_after == 5.0


def test_normalize_garbage_retry_after_without_status_is_terminal() -> None:
    # No status and an unparseable Retry-After → terminal (no retry signal survives).
    result = _normalize_sdk_error(_StubSDKError("throttled", headers={"retry-after": "later"}))
    assert isinstance(result, LLMError)
    assert not isinstance(result, TransientLLMError)


def test_retry_after_from_numeric() -> None:
    assert _retry_after_from(_StubSDKError("x", headers={"retry-after": "3.5"})) == 3.5


def test_retry_after_from_garbage_is_none() -> None:
    assert _retry_after_from(_StubSDKError("x", headers={"retry-after": "whenever"})) is None


def test_retry_after_from_missing_is_none() -> None:
    # No response object at all, and a header dict lacking the retry-after key.
    assert _retry_after_from(_StubSDKError("x")) is None
    assert _retry_after_from(_StubSDKError("x", headers={"content-type": "json"})) is None
