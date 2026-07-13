"""Cross-backend protocol contract suite (spec 0002, slice 1).

``tests/test_llm_sdk_path.py`` pins the *SDK-pair* request shape; this module pins the
:class:`Backend` protocol semantics (§16.3) that **every** backend must satisfy, so a
future backend (vertex, openai-compat, …) has a concrete checklist to pass. Each of the
four :class:`LLMBackend` values is driven keyless: ``fake`` runs for real, the two SDK
backends take an injected stub client, and ``claude_cli`` takes a stub ``_run`` seam —
no SDK import, no ``claude`` binary, no network.

The completeness pin (:func:`test_contract_covers_every_backend`) fails the gate if a
fifth backend joins :class:`LLMBackend` without joining this suite — the same enforcement
pattern as ``tests/test_config.py::test_backend_enum_matches_backend_factory``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from enterprise_sim.core.config.models import LLMBackend
from enterprise_sim.core.llm.backends import (
    AnthropicAPIBackend,
    Backend,
    BedrockBackend,
    FakeBackend,
)
from enterprise_sim.core.llm.prompt import Prompt, PromptLayer
from enterprise_sim.core.llm.types import Completion, LLMError

from tests.llm_stubs import (
    StubCLIBackend,
    StubClient,
    StubSDKError,
    backend_with_client,
    tool_use_response,
)

MODEL = "contract-model-1"

# A structured schema exercising both ``required`` and ``enum`` — the two constraints the
# minimal conformance checker verifies below.
_STRUCT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["alpha", "beta", "gamma"]},
        "count": {"type": "integer"},
    },
    "required": ["kind", "count"],
}
# A canned payload conforming to ``_STRUCT_SCHEMA`` for the backends whose output we
# inject (the SDK pair and the CLI). ``fake`` synthesizes its own conforming payload.
_STRUCT_PAYLOAD: dict[str, Any] = {"kind": "beta", "count": 3}
_CONTENT_PAYLOAD: dict[str, Any] = {"content": "the answer", "references_used": ["doc-1", "doc-2"]}
_CANDIDATES = ["doc-1", "doc-2", "doc-3"]

# Every backend the contract covers; the completeness pin ties this to ``LLMBackend``.
_CONTRACT_BACKENDS = ["fake", "anthropic_api", "bedrock", "claude_cli"]


def _prompt() -> Prompt:
    return Prompt(
        layers=(
            PromptLayer(role="system", text="system", cacheable=True, label="system"),
            PromptLayer(role="user", text="do the task", cacheable=False),
        )
    )


def _structured_backend(name: str) -> Backend:
    """A backend that returns a ``_STRUCT_SCHEMA``-conforming completion for structured mode."""
    if name == "fake":
        return FakeBackend()
    if name == "anthropic_api":
        return backend_with_client(
            AnthropicAPIBackend, StubClient(tool_use_response(dict(_STRUCT_PAYLOAD)))
        )
    if name == "bedrock":
        return backend_with_client(
            BedrockBackend, StubClient(tool_use_response(dict(_STRUCT_PAYLOAD)))
        )
    if name == "claude_cli":
        return StubCLIBackend(output=json.dumps(_STRUCT_PAYLOAD))
    raise AssertionError(f"unhandled backend {name!r}")


def _content_backend(name: str) -> Backend:
    """A backend that returns a content-envelope completion for content mode."""
    if name == "fake":
        return FakeBackend()
    if name == "anthropic_api":
        return backend_with_client(
            AnthropicAPIBackend, StubClient(tool_use_response(dict(_CONTENT_PAYLOAD)))
        )
    if name == "bedrock":
        return backend_with_client(
            BedrockBackend, StubClient(tool_use_response(dict(_CONTENT_PAYLOAD)))
        )
    if name == "claude_cli":
        return StubCLIBackend(output=json.dumps(_CONTENT_PAYLOAD))
    raise AssertionError(f"unhandled backend {name!r}")


def _assert_conforms(data: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Minimal hand-rolled conformance check: ``required`` present, ``enum`` honored.

    A full jsonschema validator is deliberately avoided (no new dep, spec 0002 §2); this
    verifies exactly the two constraints the contract asserts a backend must respect.
    """
    assert isinstance(data, dict)
    for key in schema.get("required", []):
        assert key in data, f"missing required key {key!r}"
    for key, subschema in schema.get("properties", {}).items():
        if key in data and "enum" in subschema:
            assert data[key] in subschema["enum"], f"{key!r}={data[key]!r} not in enum"


def _assert_usage_ok(completion: Completion) -> None:
    for field_value in (
        completion.usage.input_tokens,
        completion.usage.cached_input_tokens,
        completion.usage.output_tokens,
    ):
        assert isinstance(field_value, int)
        assert field_value >= 0


# ---------------------------------------------------------------------------
# Structured mode contract (generate_structured)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _CONTRACT_BACKENDS)
def test_structured_contract(name: str) -> None:
    completion = _structured_backend(name).generate_structured(
        _prompt(), schema=_STRUCT_SCHEMA, model=MODEL, temperature=0.0
    )

    assert isinstance(completion, Completion)
    assert completion.model == MODEL  # the caller's model is echoed back
    assert isinstance(completion.structured, dict)
    _assert_conforms(completion.structured, _STRUCT_SCHEMA)
    # ``text`` is the sorted-key JSON of ``structured`` in structured mode.
    assert completion.text == json.dumps(completion.structured, sort_keys=True)
    assert isinstance(completion.references_used, tuple)
    assert all(isinstance(r, str) for r in completion.references_used)
    _assert_usage_ok(completion)


# ---------------------------------------------------------------------------
# Content mode contract (generate_content)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _CONTRACT_BACKENDS)
def test_content_contract(name: str) -> None:
    completion = _content_backend(name).generate_content(
        _prompt(), candidate_references=_CANDIDATES, model=MODEL, temperature=0.5
    )

    assert isinstance(completion, Completion)
    assert completion.model == MODEL
    assert isinstance(completion.text, str)
    assert isinstance(completion.structured, dict)
    assert isinstance(completion.references_used, tuple)
    assert all(isinstance(r, str) for r in completion.references_used)
    _assert_usage_ok(completion)


# ---------------------------------------------------------------------------
# Failure contract: provider failures surface as LLMError (injectable backends)
# ---------------------------------------------------------------------------

# ``fake`` has no failure mode; only the backends whose provider layer is injectable can
# be driven into a failure, so the failure contract is scoped to those.
_INJECTABLE_BACKENDS = ["anthropic_api", "bedrock", "claude_cli"]


def _failing_backend(name: str) -> Backend:
    if name == "anthropic_api":
        return backend_with_client(
            AnthropicAPIBackend, StubClient(error=StubSDKError("bad", status_code=400))
        )
    if name == "bedrock":
        return backend_with_client(
            BedrockBackend, StubClient(error=StubSDKError("bad", status_code=400))
        )
    if name == "claude_cli":
        # Non-JSON CLI output drives ``_extract_json_object`` to raise ``LLMError``.
        return StubCLIBackend(output="this is not json at all")
    raise AssertionError(f"unhandled backend {name!r}")


@pytest.mark.parametrize("name", _INJECTABLE_BACKENDS)
def test_provider_failure_is_llm_error(name: str) -> None:
    with pytest.raises(LLMError):
        _failing_backend(name).generate_structured(
            _prompt(), schema=_STRUCT_SCHEMA, model=MODEL, temperature=0.0
        )


# ---------------------------------------------------------------------------
# Completeness pin
# ---------------------------------------------------------------------------


def test_contract_covers_every_backend() -> None:
    # Adding a backend to ``LLMBackend`` without adding it here fails the gate — the same
    # enforcement as ``test_config.test_backend_enum_matches_backend_factory``.
    assert set(_CONTRACT_BACKENDS) == {backend.value for backend in LLMBackend}
