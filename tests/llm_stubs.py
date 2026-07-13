"""Shared, duck-typed stubs for exercising the LLM backends keyless (spec 0001/0002).

The official SDK client is duck-typed ``Any`` in :class:`_AnthropicSDKBackend` (§7), so
a tiny stub exposing ``messages.create`` drives the whole request path with **no SDK
import and no network**. Likewise :class:`ClaudeCLIBackend` funnels its subprocess call
through a single ``_run`` seam, so overriding that one method drives the CLI parse path
offline. These helpers back both ``tests/test_llm_sdk_path.py`` (SDK request-shape pins)
and ``tests/test_backend_contract.py`` (the cross-backend protocol contract suite).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from enterprise_sim.core.llm.backends import (
    ClaudeCLIBackend,
    _AnthropicSDKBackend,
)
from enterprise_sim.core.llm.prompt import Prompt, PromptLayer


class StubMessages:
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


class StubClient:
    """Stand-in for ``anthropic.Anthropic`` / ``AnthropicBedrock`` (duck-typed ``Any``)."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.messages = StubMessages(response, error)


class StubSDKError(Exception):
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


class StubCLIBackend(ClaudeCLIBackend):
    """CLI backend whose subprocess ``_run`` seam is replaced by a canned string / error.

    Overriding only ``_run`` (the genuinely CLI-requiring seam) drives the inherited
    parse path — ``generate_structured``/``generate_content``/``_extract_json_object`` —
    against realistic canned output with no ``claude`` binary.
    """

    def __init__(self, *, output: str | None = None, error: Exception | None = None) -> None:
        super().__init__()
        self._output = output
        self._error = error

    def _run(self, prompt_text: str, model: str) -> str:
        if self._error is not None:
            raise self._error
        assert self._output is not None
        return self._output


def tool_use_response(tool_input: dict[str, Any], usage: Any = None) -> SimpleNamespace:
    """A canned ``messages.create`` response carrying a single ``tool_use`` block."""
    block = SimpleNamespace(type="tool_use", input=tool_input)
    return SimpleNamespace(content=[block], usage=usage if usage is not None else SimpleNamespace())


def backend_with_client(
    cls: type[_AnthropicSDKBackend], client: Any, *, max_tokens: int = 4096
) -> _AnthropicSDKBackend:
    """Construct ``cls`` and inject a stub client via the ``_client`` cache slot."""
    backend = cls(max_tokens=max_tokens)
    backend._client = client
    return backend


def simple_prompt() -> Prompt:
    """A minimal cacheable-system + user prompt."""
    return Prompt(
        layers=(
            PromptLayer(role="system", text="S", cacheable=True, label="system"),
            PromptLayer(role="user", text="U"),
        )
    )
