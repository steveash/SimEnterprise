"""Shared value types and errors for the LLM layer (ARCHITECTURE.md §7/§16).

These are the small, immutable contracts every other module in ``core.llm``
speaks: token/cost bookkeeping (:class:`TokenUsage`), the backend return value
(:class:`Completion`), the two public result shapes (:class:`StructuredResult`,
:class:`ContentResult`), and the error hierarchy that drives retry and the cost
ceiling. Keeping them in one dependency-free module lets backends, the cache,
the pricing table, and the client all import them without cycles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Per-call token counts split for cost accounting (§16.4).

    ``cached_input_tokens`` are input tokens served from prompt cache; they are
    billed at the cheaper cached rate, so they are tracked separately rather
    than folded into ``input_tokens``. Backends that cannot report a breakdown
    (e.g. ``claude_cli``) leave the fields they don't know at ``0``.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    @property
    def total_tokens(self) -> int:
        """Total billable tokens (uncached input + cached input + output)."""
        return self.input_tokens + self.cached_input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        """Return a JSON-serializable mapping of this usage."""
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TokenUsage:
        """Reconstruct :class:`TokenUsage` from :meth:`to_dict` output."""
        return cls(
            input_tokens=int(data.get("input_tokens", 0)),
            cached_input_tokens=int(data.get("cached_input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
        )


@dataclass(frozen=True, slots=True)
class Completion:
    """The raw result of one backend call, before client-side bookkeeping.

    ``text`` is the prose body (empty for a pure structured call). ``structured``
    holds the parsed object for ``generate_structured`` / the content envelope
    for ``generate_content``. ``references_used`` is the model's *claimed* set of
    cited artifact ids — the client verifies it against the candidate set (D32)
    before exposing it. ``cache_hit`` is set by the client when the value came
    from the on-disk response cache (D31), not the backend.
    """

    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    structured: dict[str, Any] | None = None
    references_used: tuple[str, ...] = ()
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (used by the response cache)."""
        return {
            "text": self.text,
            "usage": self.usage.to_dict(),
            "model": self.model,
            "structured": self.structured,
            "references_used": list(self.references_used),
            "cache_hit": self.cache_hit,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Completion:
        """Reconstruct a :class:`Completion` from :meth:`to_dict` output."""
        return cls(
            text=data["text"],
            usage=TokenUsage.from_dict(data.get("usage", {})),
            model=data.get("model", ""),
            structured=data.get("structured"),
            references_used=tuple(data.get("references_used", ())),
            cache_hit=bool(data.get("cache_hit", False)),
        )


@dataclass(frozen=True, slots=True)
class StructuredResult:
    """Public result of :meth:`LLMClient.generate_structured`."""

    data: dict[str, Any]
    usage: TokenUsage
    model: str
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class ContentResult:
    """Public result of :meth:`LLMClient.generate_content` (§16.3).

    ``references_used`` is already *verified* — every id is a member of the
    candidate set the caller supplied; hallucinated citations are dropped.
    """

    content: str
    references_used: tuple[str, ...]
    usage: TokenUsage
    model: str
    cache_hit: bool


class LLMError(Exception):
    """Base class for every error raised by the LLM layer."""


class TransientLLMError(LLMError):
    """A retryable failure (rate limit, 5xx, timeout).

    ``retry_after`` mirrors the HTTP ``Retry-After`` header when the provider
    supplied one; the client's backoff honors it instead of its own schedule.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CostCeilingExceeded(LLMError):
    """Raised when a call would push accumulated spend past the configured ceiling."""

    def __init__(self, projected_usd: float, ceiling_usd: float) -> None:
        super().__init__(f"projected spend ${projected_usd:.4f} exceeds ceiling ${ceiling_usd:.4f}")
        self.projected_usd = projected_usd
        self.ceiling_usd = ceiling_usd


class ReferenceVerificationError(LLMError):
    """Raised when no claimed reference survives verification against the candidate set."""


def verify_references(claimed: Sequence[str], candidates: Sequence[str]) -> tuple[str, ...]:
    """Return the claimed references that are members of the candidate set (D32).

    Order follows ``claimed`` (the model's citation order) but each id appears at
    most once. Hallucinated ids — anything not in ``candidates`` — are silently
    dropped; the caller decides whether an empty result is acceptable.
    """
    allowed = set(candidates)
    seen: set[str] = set()
    kept: list[str] = []
    for ref in claimed:
        if ref in allowed and ref not in seen:
            seen.add(ref)
            kept.append(ref)
    return tuple(kept)
