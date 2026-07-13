"""Shared, duck-typed stubs for exercising the LLM backends keyless (spec 0001/0002).

The official SDK client is duck-typed ``Any`` in :class:`_AnthropicSDKBackend` (§7), so
a tiny stub exposing ``messages.create`` drives the whole request path with **no SDK
import and no network**. Likewise :class:`ClaudeCLIBackend` funnels its subprocess call
through a single ``_run`` seam, so overriding that one method drives the CLI parse path
offline. These helpers back both ``tests/test_llm_sdk_path.py`` (SDK request-shape pins)
and ``tests/test_backend_contract.py`` (the cross-backend protocol contract suite).

This module also owns the **cassette record/replay** infrastructure (spec 0002 §1). A
cassette *is* the existing on-disk :class:`~enterprise_sim.core.llm.cache.ResponseCache`
pointed at a committed directory: a cache hit replays a recorded :class:`Completion`
(the warm-cache production path — ``client.py:349-354``), so replay is itself D31
coverage. :class:`CassetteMissBackend` turns any miss into a terminal failure naming the
drifted request and the re-record command, so a stale/absent recording fails loudly
instead of silently hitting the network. Recording is a manual, keyed, cost-capped act
driven by ``ESIM_CASSETTES=record``; the redaction scan is the belt-and-braces over
``Completion.to_dict`` (which cannot carry a credential by construction).
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from enterprise_sim.core.llm.backends import (
    ClaudeCLIBackend,
    _AnthropicSDKBackend,
)
from enterprise_sim.core.llm.cache import request_key
from enterprise_sim.core.llm.client import LLMClient, LLMConfig, build_client
from enterprise_sim.core.llm.prompt import Prompt, PromptLayer
from enterprise_sim.core.llm.types import Completion, LLMError
from enterprise_sim.reconstruct.extract import HAIKU_MODEL


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


# ---------------------------------------------------------------------------
# Cassette record/replay (spec 0002 §1)
# ---------------------------------------------------------------------------

# Committed cassette root; scenario recordings live in ``<CASSETTE_ROOT>/<scenario>/``.
CASSETTE_ROOT = Path(__file__).resolve().parent / "cassettes"

# The one command that (re)records every scenario cassette; surfaced in every skip and
# miss message so a keyless contributor always knows how to refresh a drifted recording.
RECORD_COMMAND = "ESIM_CASSETTES=record uv run pytest tests/test_llm_cassettes.py"


def recording_enabled() -> bool:
    """Whether the session is in record mode (``ESIM_CASSETTES=record``).

    Read at call time (not a module constant) so a test can drive both branches with
    ``monkeypatch.setenv``. Record mode is manual, keyed, and never runs in CI.
    """
    return os.environ.get("ESIM_CASSETTES") == "record"


class CassetteMissBackend:
    """A replay-only backend whose every call is a cassette miss (spec 0002 §1).

    Under strict replay the :class:`~enterprise_sim.core.llm.cache.ResponseCache`
    serves recorded completions and this backend is never reached
    (``client.py:349-354``). Reaching it therefore *means* the cassette has no entry
    for the request — the prompt, schema, model, or temperature drifted from the
    recording, or the scenario was never recorded. It raises a **terminal**
    :class:`LLMError` (not :class:`~enterprise_sim.core.llm.types.TransientLLMError`, so
    the client's retry loop does not spin — ``client.py:369-377``) naming the missing
    request key and the re-record command. It lives in tests, not core: the ``Backend``
    protocol is duck-typed (``backends.py:38-69``), so no ``enterprise_sim/**`` change is
    needed.
    """

    name = "cassette-miss"

    def generate_structured(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
    ) -> Completion:
        raise self._miss(
            request_key(
                prompt=prompt,
                model=model,
                mode="structured",
                schema=schema,
                temperature=temperature,
            )
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        raise self._miss(
            request_key(
                prompt=prompt,
                model=model,
                mode="content",
                candidates=tuple(candidate_references),
                temperature=temperature,
            )
        )

    @staticmethod
    def _miss(key: str) -> LLMError:
        return LLMError(
            f"cassette miss for request {key}: no recorded completion for this request. "
            "The prompt/schema/model/temperature drifted from the recording, or the "
            f"scenario was never recorded. Re-record with: {RECORD_COMMAND}"
        )


def replay_client(cassette_dir: Path) -> LLMClient:
    """A strict replay client over ``cassette_dir`` (no network, no key).

    ``cache_dir`` points at the committed cassette directory, so a warm hit replays the
    recorded completion through the production cache path (D31); a miss reaches
    :class:`CassetteMissBackend` and fails terminally. The direct constructor bypasses
    ``build_backend`` so the tests-only backend needs no registry entry.
    """
    return LLMClient(
        CassetteMissBackend(),
        config=LLMConfig(
            backend="cassette-replay",
            model=HAIKU_MODEL,
            cache_dir=str(cassette_dir),
            cache_enabled=True,
        ),
    )


def cassette_client(cassette_dir: Path) -> LLMClient:
    """The scenario client: strict replay by default, keyed recording under record mode.

    Replay (default) returns :func:`replay_client`. Record mode
    (``ESIM_CASSETTES=record``) returns a keyed ``anthropic_api`` client writing the
    cassette to ``cassette_dir`` with a ``$1`` cost ceiling (D13); if the key or the
    ``anthropic`` SDK (``--extra bench``) is missing it :func:`pytest.skip`\\ s with the
    re-record command rather than erroring. Both branches pin :data:`HAIKU_MODEL` and the
    shared :class:`LLMConfig` temperature defaults so record and replay compute identical
    ``request_key``\\ s.
    """
    if recording_enabled():
        if not (os.environ.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic")):
            pytest.skip(
                "recording cassettes needs ANTHROPIC_API_KEY + the anthropic SDK "
                f"(uv sync --extra bench); re-record with: {RECORD_COMMAND}"
            )
        return build_client(
            LLMConfig(
                backend="anthropic_api",
                model=HAIKU_MODEL,
                cache_dir=str(cassette_dir),
                cost_ceiling_usd=1.0,
            )
        )
    return replay_client(cassette_dir)


def require_cassette(cassette_dir: Path) -> None:
    """Skip the calling replay test when ``cassette_dir`` is absent or empty.

    This is the skip-if-unrecorded state: after the infrastructure lands but before the
    (keyed) owner records, the keyless gate stays green by skipping with the record
    command. Record mode never skips here — the directory is about to be (re)created.
    """
    if recording_enabled():
        return
    if not cassette_dir.is_dir() or not any(cassette_dir.glob("*.json")):
        pytest.skip(f"no cassette recorded at {cassette_dir}; record with: {RECORD_COMMAND}")


def scan_cassette_for_secrets(cassette_dir: Path) -> None:
    """Fail if any cassette file in ``cassette_dir`` leaks a credential (record belt-and-braces).

    ``Completion.to_dict`` records only text/usage/model/structured/references_used, so a
    credential cannot be present by construction (spec 0002 §1). Record mode scans every
    written file for the live ``ANTHROPIC_API_KEY`` value and the ``sk-ant-`` prefix
    anyway, so a future serialization change can never silently commit a secret. Raises
    :class:`LLMError` naming the offending file.
    """
    live_key = os.environ.get("ANTHROPIC_API_KEY")
    for path in sorted(cassette_dir.glob("*.json")):
        text = path.read_text()
        if "sk-ant-" in text or (live_key and live_key in text):
            raise LLMError(
                f"cassette {path} contains a credential-shaped secret; recording aborted. "
                "This should be impossible via Completion.to_dict — investigate before "
                "committing."
            )
