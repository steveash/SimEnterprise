"""Layered, cache-aware prompt assembly (ARCHITECTURE.md §16.1, D29).

A prompt is built **stable→volatile** so prompt caching pays off across the many
artifacts of a run::

    [ system prompt           ]  per artifact-kind   ┐
    [ company profile          ]  per run            │ cacheable prefix
    [ scenario/project context ]  per scenario       ┘ (≤4 cache breakpoints)
    ───────────────────────────── cache_control breakpoint
    [ task brief + roster      ]  per artifact        volatile suffix

This module owns only the *shape*. Templates are owned by each generator/producer
plugin (§16.1); this is shared infra. Backends translate :class:`Prompt` into
whatever their SDK wants — the SDK paths attach ``cache_control`` to the cacheable
layers; ``claude_cli`` flattens everything to text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

# Anthropic permits at most four ``cache_control`` breakpoints per request.
MAX_CACHE_BREAKPOINTS = 4


@dataclass(frozen=True, slots=True)
class PromptLayer:
    """One ordered block of a prompt.

    ``role`` is ``"system"`` or ``"user"``. ``cacheable`` marks a stable prefix
    block that should carry ``cache_control`` on SDK backends. ``label`` is a
    human tag (``"company_profile"``) for debugging and never sent to the model.
    """

    role: str
    text: str
    cacheable: bool = False
    label: str = ""


@dataclass(frozen=True, slots=True)
class Prompt:
    """An assembled, ordered prompt: cacheable system prefix then volatile suffix."""

    layers: tuple[PromptLayer, ...]

    @property
    def system_layers(self) -> tuple[PromptLayer, ...]:
        """The ``system``-role layers, in assembly order."""
        return tuple(layer for layer in self.layers if layer.role == "system")

    @property
    def user_layers(self) -> tuple[PromptLayer, ...]:
        """The ``user``-role layers, in assembly order."""
        return tuple(layer for layer in self.layers if layer.role == "user")

    @property
    def cacheable_layers(self) -> tuple[PromptLayer, ...]:
        """The stable prefix layers that should carry ``cache_control``."""
        return tuple(layer for layer in self.layers if layer.cacheable)

    @property
    def system_text(self) -> str:
        """All system layers joined — the flattened system prompt."""
        return "\n\n".join(layer.text for layer in self.system_layers)

    @property
    def user_text(self) -> str:
        """All user layers joined — the flattened user message."""
        return "\n\n".join(layer.text for layer in self.user_layers)

    @property
    def text(self) -> str:
        """The full prompt flattened to text (for ``claude_cli``, fake, hashing)."""
        return "\n\n".join(layer.text for layer in self.layers)

    def hash(self) -> str:
        """A stable content hash of the whole prompt (basis for cache keys, D31).

        Covers role, cacheability, and text of every layer in order, so two
        prompts that differ only in a volatile brief get different hashes while
        identical prompts (the common cross-artifact case) collide intentionally.
        """
        h = hashlib.sha256()
        for layer in self.layers:
            h.update(layer.role.encode())
            h.update(b"\x00")
            h.update(b"1" if layer.cacheable else b"0")
            h.update(b"\x00")
            h.update(layer.text.encode())
            h.update(b"\x1e")  # record separator between layers
        return h.hexdigest()


def assemble_prompt(
    *,
    system: str,
    stable_context: Sequence[str] = (),
    brief: str,
    labels: Sequence[str] = (),
) -> Prompt:
    """Assemble a layered prompt (D29).

    ``system`` is the per-artifact-kind system prompt (cacheable). Each entry in
    ``stable_context`` (company profile, scenario/project context, …) becomes a
    cacheable system layer. ``brief`` is the volatile per-artifact suffix (task
    brief + roster) and is *not* cacheable. ``labels`` optionally name the
    ``stable_context`` blocks for debugging.

    Raises ``ValueError`` if the cacheable prefix would exceed
    :data:`MAX_CACHE_BREAKPOINTS` (the system prompt plus the stable blocks),
    surfacing the §16.1 constraint at assembly time rather than at the API.
    """
    layers: list[PromptLayer] = [
        PromptLayer(role="system", text=system, cacheable=True, label="system")
    ]
    for i, block in enumerate(stable_context):
        label = labels[i] if i < len(labels) else f"context_{i}"
        layers.append(PromptLayer(role="system", text=block, cacheable=True, label=label))

    cacheable = sum(1 for layer in layers if layer.cacheable)
    if cacheable > MAX_CACHE_BREAKPOINTS:
        raise ValueError(
            f"prompt has {cacheable} cacheable layers; "
            f"at most {MAX_CACHE_BREAKPOINTS} cache breakpoints are allowed"
        )

    layers.append(PromptLayer(role="user", text=brief, cacheable=False, label="brief"))
    return Prompt(layers=tuple(layers))
