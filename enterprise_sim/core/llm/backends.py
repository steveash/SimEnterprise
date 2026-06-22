"""LLM backends: ``fake`` · ``anthropic_api`` · ``bedrock`` · ``claude_cli`` (§7, §16.4).

Every backend implements the same two-method :class:`Backend` protocol — one for
each generation mode (§16.3). The client layers retry, concurrency, cost, and the
response cache on top; a backend only has to turn a :class:`Prompt` into a
:class:`Completion`.

* :class:`FakeBackend` — deterministic templated output, **no network** (D31). It
  is what the §13 test kit runs on: same prompt → byte-identical result, free.
* :class:`AnthropicAPIBackend` / :class:`BedrockBackend` — the *same* official SDK,
  two constructors ("key vs Bedrock is one dependency, two constructors", §7).
* :class:`ClaudeCLIBackend` — shells out to ``claude -p --output-format json`` to
  route bulk fan-out through the OAuth subscription.

The SDK and CLI are **lazily imported inside the methods** so importing this module
(and running the deterministic tests) never requires the ``anthropic`` package or
the ``claude`` binary to be installed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from enterprise_sim.core.llm.pricing import DEFAULT_MODEL
from enterprise_sim.core.llm.prompt import Prompt
from enterprise_sim.core.llm.types import (
    Completion,
    LLMError,
    TokenUsage,
    TransientLLMError,
)


@runtime_checkable
class Backend(Protocol):
    """The provider-agnostic interface every backend implements.

    Both methods are synchronous and *stateless* per call; the client owns all
    cross-cutting concerns. ``temperature`` follows §16.3 (low for structured,
    higher for prose) but is passed explicitly so the client/config decides.
    """

    name: str

    def generate_structured(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
    ) -> Completion:
        """Return a completion whose ``structured`` conforms to ``schema`` (§16.3)."""
        ...

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        """Return prose plus the model's *claimed* ``references_used`` (§16.3)."""
        ...


def estimate_tokens(text: str) -> int:
    """A rough deterministic token estimate (~4 chars/token), min 1.

    Used by the fake backend and dry-run estimates. Never zero, so an empty
    string still costs one token and cost arithmetic stays well-defined.
    """
    return max(1, len(text) // 4)


def _seed(*parts: str) -> str:
    """A short stable hex seed derived from ``parts`` (deterministic)."""
    h = hashlib.sha256("\x00".join(parts).encode())
    return h.hexdigest()[:12]


def _fake_value(schema: Mapping[str, Any], seed: str) -> Any:
    """Deterministically synthesize a value conforming to a (subset of) JSON Schema.

    Supports ``object``/``array``/``string``/``integer``/``number``/``boolean``
    and ``enum`` — enough for world-building attributes and artifact outlines
    (§16.3). The same ``(schema, seed)`` always yields the same value, which is
    what makes the fake backend reproducible.
    """
    if "enum" in schema:
        choices = list(schema["enum"])
        if choices:
            idx = int(seed[:8], 16) % len(choices)
            return choices[idx]
    schema_type = schema.get("type")
    if schema_type == "object":
        props: Mapping[str, Any] = schema.get("properties", {})
        return {key: _fake_value(sub, _seed(seed, key)) for key, sub in props.items()}
    if schema_type == "array":
        item_schema: Mapping[str, Any] = schema.get("items", {"type": "string"})
        # One deterministic element keeps output small but exercises array paths.
        return [_fake_value(item_schema, _seed(seed, "0"))]
    if schema_type == "integer":
        return int(seed[:6], 16) % 100
    if schema_type == "number":
        return round((int(seed[:6], 16) % 1000) / 10.0, 1)
    if schema_type == "boolean":
        return int(seed[:2], 16) % 2 == 0
    # Default / ``string``.
    return f"fake-{seed[:8]}"


class FakeBackend:
    """Deterministic, network-free backend for the test kit (D31).

    Structured output is synthesized from the schema; prose is a templated echo
    of the volatile brief. ``references_used`` is a deterministic subset of the
    candidate set, so reference verification (D32) has something real to check.
    """

    name = "fake"

    def generate_structured(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
    ) -> Completion:
        seed = _seed(prompt.hash(), model)
        data = _fake_value(schema, seed)
        if not isinstance(data, dict):
            # A non-object top-level schema is wrapped so callers always get a dict.
            data = {"value": data}
        text = json.dumps(data, sort_keys=True)
        usage = TokenUsage(
            input_tokens=estimate_tokens(prompt.user_text),
            cached_input_tokens=estimate_tokens(prompt.system_text),
            output_tokens=estimate_tokens(text),
        )
        return Completion(text=text, usage=usage, model=model, structured=data)

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        seed = _seed(prompt.hash(), model)
        content = f"[fake:{model}:{seed[:6]}] {prompt.user_text}".strip()
        # Deterministically "cite" roughly half the candidates, order preserved.
        refs = tuple(ref for ref in candidate_references if int(_seed(seed, ref)[:2], 16) % 2 == 0)
        usage = TokenUsage(
            input_tokens=estimate_tokens(prompt.user_text),
            cached_input_tokens=estimate_tokens(prompt.system_text),
            output_tokens=estimate_tokens(content),
        )
        return Completion(
            text=content,
            usage=usage,
            model=model,
            structured={"content": content, "references_used": list(refs)},
            references_used=refs,
        )


# JSON schema for the prose envelope the SDK/CLI backends force the model to emit.
_CONTENT_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "references_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["content"],
}


def _usage_from_sdk(raw_usage: Any) -> TokenUsage:
    """Map an Anthropic SDK ``usage`` object to :class:`TokenUsage` (§16.4).

    The SDK reports ``input_tokens`` excluding cache reads plus a separate
    ``cache_read_input_tokens``; we keep them split so cached tokens bill at the
    cheaper rate. ``getattr`` with defaults tolerates SDK-version drift.
    """
    return TokenUsage(
        input_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
        cached_input_tokens=int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0),
        output_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
    )


class _AnthropicSDKBackend:
    """Shared implementation for the two official-SDK backends (§7).

    Subclasses only differ in how they construct the SDK client (key vs Bedrock).
    Both use tool-use forced output for structured generation: a single tool whose
    ``input_schema`` is the caller's schema, with ``tool_choice`` pinning it, so
    the model is obliged to return schema-shaped JSON.
    """

    name = "anthropic_sdk"

    def __init__(self, *, max_tokens: int = 4096) -> None:
        self._max_tokens = max_tokens
        self._client: Any | None = None

    def _make_client(self) -> Any:  # pragma: no cover - requires the SDK
        raise NotImplementedError

    def _client_or_create(self) -> Any:  # pragma: no cover - requires the SDK
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _system_blocks(self, prompt: Prompt) -> list[dict[str, Any]]:  # pragma: no cover
        """System blocks with ``cache_control`` on each cacheable layer (§16.1)."""
        blocks: list[dict[str, Any]] = []
        for layer in prompt.system_layers:
            block: dict[str, Any] = {"type": "text", "text": layer.text}
            if layer.cacheable:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        return blocks

    def _messages(self, prompt: Prompt) -> list[dict[str, Any]]:  # pragma: no cover
        return [{"role": "user", "content": prompt.user_text}]

    def _call_tool(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
        tool_name: str,
    ) -> tuple[dict[str, Any], TokenUsage]:  # pragma: no cover - requires the SDK
        client = self._client_or_create()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                temperature=temperature,
                system=self._system_blocks(prompt),
                tools=[
                    {
                        "name": tool_name,
                        "description": "Return the requested structured output.",
                        "input_schema": dict(schema),
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                messages=self._messages(prompt),
            )
        except Exception as exc:  # noqa: BLE001 - normalized into our hierarchy
            raise _normalize_sdk_error(exc) from exc
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input), _usage_from_sdk(response.usage)
        raise LLMError("anthropic SDK returned no tool_use block")

    def generate_structured(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
    ) -> Completion:  # pragma: no cover - requires the SDK
        data, usage = self._call_tool(
            prompt,
            schema=schema,
            model=model,
            temperature=temperature,
            tool_name="emit_structured_output",
        )
        return Completion(
            text=json.dumps(data, sort_keys=True),
            usage=usage,
            model=model,
            structured=data,
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:  # pragma: no cover - requires the SDK
        data, usage = self._call_tool(
            prompt,
            schema=_CONTENT_ENVELOPE_SCHEMA,
            model=model,
            temperature=temperature,
            tool_name="emit_content",
        )
        content = str(data.get("content", ""))
        refs = tuple(str(r) for r in data.get("references_used", []))
        return Completion(
            text=content,
            usage=usage,
            model=model,
            structured=data,
            references_used=refs,
        )


class AnthropicAPIBackend(_AnthropicSDKBackend):
    """Official SDK + ``ANTHROPIC_API_KEY`` (§7). Supports prompt caching."""

    name = "anthropic_api"

    def _make_client(self) -> Any:  # pragma: no cover - requires the SDK
        import importlib

        anthropic = importlib.import_module("anthropic")
        return anthropic.Anthropic()


class BedrockBackend(_AnthropicSDKBackend):
    """Same official SDK via ``AnthropicBedrock`` (§7) — one dependency, two constructors."""

    name = "bedrock"

    def _make_client(self) -> Any:  # pragma: no cover - requires the SDK
        import importlib

        anthropic = importlib.import_module("anthropic")
        return anthropic.AnthropicBedrock()


class ClaudeCLIBackend:
    """Shell out to ``claude -p --output-format json`` (§7).

    Routes bulk fan-out through the OAuth subscription. The CLI gives less control
    over cache-control and token accounting (§7 caveat), so usage is *estimated*
    from text length and the cacheable prefix is still ordered first to benefit
    the SDK paths that share these prompts.
    """

    name = "claude_cli"

    def __init__(self, *, binary: str = "claude", timeout: float = 120.0) -> None:
        self._binary = binary
        self._timeout = timeout

    def _run(self, prompt_text: str, model: str) -> str:  # pragma: no cover - needs CLI
        try:
            proc = subprocess.run(
                [self._binary, "-p", prompt_text, "--output-format", "json", "--model", model],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientLLMError(f"claude CLI timed out after {self._timeout}s") from exc
        except OSError as exc:
            raise LLMError(f"claude CLI not runnable: {exc}") from exc
        if proc.returncode != 0:
            raise TransientLLMError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude CLI returned non-JSON: {exc}") from exc
        # ``--output-format json`` wraps the assistant text under "result".
        return str(envelope.get("result", proc.stdout))

    def generate_structured(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
    ) -> Completion:  # pragma: no cover - needs CLI
        instruction = (
            f"{prompt.text}\n\nReturn ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema, sort_keys=True)}"
        )
        raw = self._run(instruction, model)
        data = _extract_json_object(raw)
        return Completion(
            text=json.dumps(data, sort_keys=True),
            usage=_estimated_usage(prompt, raw),
            model=model,
            structured=data,
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:  # pragma: no cover - needs CLI
        instruction = (
            f"{prompt.text}\n\nReturn ONLY a JSON object: "
            '{"content": <prose>, "references_used": [<artifact ids you cited>]}'
        )
        raw = self._run(instruction, model)
        data = _extract_json_object(raw)
        content = str(data.get("content", raw))
        refs = tuple(str(r) for r in data.get("references_used", []))
        return Completion(
            text=content,
            usage=_estimated_usage(prompt, content),
            model=model,
            structured=data,
            references_used=refs,
        )


def _estimated_usage(prompt: Prompt, output: str) -> TokenUsage:  # pragma: no cover
    return TokenUsage(
        input_tokens=estimate_tokens(prompt.user_text),
        cached_input_tokens=estimate_tokens(prompt.system_text),
        output_tokens=estimate_tokens(output),
    )


def _extract_json_object(raw: str) -> dict[str, Any]:  # pragma: no cover - needs CLI
    """Best-effort parse of a JSON object from CLI text (tolerates surrounding prose)."""
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise LLMError("could not extract a JSON object from claude CLI output")


def _normalize_sdk_error(exc: Exception) -> LLMError:  # pragma: no cover - requires the SDK
    """Map an SDK exception to our hierarchy, extracting ``Retry-After`` when present.

    Rate limits (429) and server errors (5xx) become :class:`TransientLLMError`
    so the client retries; everything else is a terminal :class:`LLMError`.
    """
    status = getattr(exc, "status_code", None)
    retry_after = _retry_after_from(exc)
    if status == 429 or (isinstance(status, int) and 500 <= status < 600):
        return TransientLLMError(str(exc), retry_after=retry_after)
    if retry_after is not None:
        return TransientLLMError(str(exc), retry_after=retry_after)
    return LLMError(str(exc))


def _retry_after_from(exc: Exception) -> float | None:  # pragma: no cover - requires the SDK
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_backend(name: str, **kwargs: Any) -> Backend:
    """Construct a backend by config name (§7: backend is config).

    ``fake`` is always available and dependency-free; the others lazily import
    their SDK/CLI only when actually used. Raises ``ValueError`` on an unknown
    name so a config typo fails loudly.
    """
    if name == "fake":
        return FakeBackend()
    if name == "anthropic_api":
        return AnthropicAPIBackend(**kwargs)
    if name == "bedrock":
        return BedrockBackend(**kwargs)
    if name == "claude_cli":
        return ClaudeCLIBackend(**kwargs)
    raise ValueError(
        f"unknown LLM backend {name!r}; expected one of "
        "['anthropic_api', 'bedrock', 'claude_cli', 'fake']"
    )


__all__ = [
    "DEFAULT_MODEL",
    "AnthropicAPIBackend",
    "Backend",
    "BedrockBackend",
    "ClaudeCLIBackend",
    "FakeBackend",
    "build_backend",
    "estimate_tokens",
]
