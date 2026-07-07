"""Schema-guided typed extraction: :class:`Chunk` → mentions + candidate triples.

The reconstruct pipeline's extraction stage (esim-nc6.3). For one
:class:`~enterprise_sim.reconstruct.schema.Chunk` it asks an LLM for the entity
mentions and relations *stated in that chunk*, constrained to the known
:mod:`~enterprise_sim.reconstruct.ontology`: mention types are drawn from
:data:`~enterprise_sim.reconstruct.ontology.NODE_TYPES` and relation labels from
:data:`~enterprise_sim.reconstruct.ontology.RELATION_TYPES`. Because we own the
target schema, this is *closed* extraction — the model fills a fixed shape rather
than inventing types — which is what makes reconstruction consistent enough to
score against the gold graph.

The stage is built on :mod:`enterprise_sim.core.llm`, so it inherits that layer's
backends unchanged:

* **Keyless** (tests, CI): the deterministic ``fake`` backend synthesizes a
  schema-shaped envelope with no network and no key — enough to exercise prompt
  assembly, the forced schema, and output parsing. The schema's ``enum`` fields
  mean the fake envelope's types/relations are always valid ontology vocabulary.
* **Keyed**: the ``anthropic_api`` backend with the cheap :data:`HAIKU_MODEL`
  extracts from real chunks. This path needs ``ANTHROPIC_API_KEY`` and is gated
  off in keyless CI (the caller selects the backend; see :func:`extract_chunk`).

Two design choices keep the output trustworthy:

* **We locate spans, not the model.** The model returns each mention's
  ``surface_form`` (and type) but *no character offsets* — LLMs are unreliable at
  character arithmetic. :func:`parse_extraction` finds the surface form in the
  chunk text to fill :attr:`~enterprise_sim.reconstruct.schema.MentionSpan.start` /
  ``end``; a form the model invented that is absent from the chunk is kept but
  marked *unlocated* (``start == end == -1``) rather than silently mis-placed.
* **Off-vocabulary output is dropped.** Any mention type or relation label outside
  the ontology is discarded during parsing, so a downstream stage only ever sees
  gold-vocabulary types — a type-level guarantee the ``enum`` schema already asks
  the backend to honor, re-checked here because a real model can still stray.

Parsing (:func:`parse_extraction`) is a pure function of ``(chunk, envelope)``, so
the whole structure — schema shape, span location, vocabulary validation, dedup —
is unit-tested against a hand-built envelope without any backend at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise_sim.core.llm import LLMClient, Prompt, assemble_prompt
from enterprise_sim.reconstruct.ontology import (
    NODE_TYPES,
    RELATION_TYPES,
    describe_ontology,
)
from enterprise_sim.reconstruct.schema import CandidateTriple, Chunk, MentionSpan

__all__ = [
    "EXTRACTION_SCHEMA",
    "HAIKU_MODEL",
    "Extraction",
    "build_extraction_prompt",
    "extract_chunk",
    "extract_chunks",
    "parse_extraction",
]

#: The cheap default model for the keyed extraction path (§7 pricing table).
HAIKU_MODEL = "claude-haiku-4-5"

#: Offset sentinel for a mention whose surface form was not found in the chunk.
_UNLOCATED = -1


@dataclass(frozen=True)
class Extraction:
    """The typed extraction result for a single :class:`Chunk`.

    Attributes:
        chunk_id: The :class:`Chunk` this extraction came from.
        mentions: The typed entity mentions found in the chunk, in model order
            with exact duplicates removed.
        triples: The candidate relations found in the chunk, each carrying the
            chunk id as provenance, in model order with exact duplicates removed.
    """

    chunk_id: str
    mentions: tuple[MentionSpan, ...] = ()
    triples: tuple[CandidateTriple, ...] = ()


# --------------------------------------------------------------------------- #
# Forced-output schema.
# --------------------------------------------------------------------------- #

# The schema the backend is forced to fill (``generate_structured``). Mention
# ``type`` and triple ``rel`` are ``enum``-constrained to the ontology, so even the
# deterministic ``fake`` backend — which picks enum values from the schema — yields
# valid vocabulary; parsing re-checks the same constraint for real backends. No
# offsets are requested: spans are located from the surface form during parsing.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface_form": {"type": "string"},
                    "type": {"type": "string", "enum": sorted(NODE_TYPES)},
                },
                "required": ["surface_form", "type"],
            },
        },
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "rel": {"type": "string", "enum": sorted(RELATION_TYPES)},
                    "dst": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["src", "rel", "dst"],
            },
        },
    },
    "required": ["mentions", "triples"],
}


_SYSTEM_PROMPT = (
    "You extract a knowledge graph from enterprise documents. Read one chunk of "
    "text and return the entity mentions and the relations it states, using ONLY "
    "the fixed ontology below — never invent entity types or relation labels.\n\n"
    f"{describe_ontology()}\n\n"
    "Rules:\n"
    "- Emit a mention for each distinct entity the chunk names, with its exact "
    "surface form (verbatim substring of the chunk) and one ontology entity type. "
    "The one exception is the Section owner named in a goal-hierarchy rule below: "
    "an owner named only in the Section breadcrumb may be emitted using that name.\n"
    "- Goals are entities too. When the chunk states a company or department "
    "objective — a bullet under a Goals heading, or a line under an Advances-goals "
    "list — emit it as a Goal mention whose surface form is the COMPLETE statement "
    "sentence, copied verbatim (keep trailing punctuation; drop only surrounding "
    'markdown like "**"). Emit EVERY goal line this way, whether or not it is bold: '
    "a plain (non-bold) indented bullet nested beneath a goal bullet is itself a "
    "Goal, not decoration.\n"
    "- subgoal_of: when a goal bullet is indented directly beneath another goal "
    "bullet, emit subgoal_of from the inner (sub-)goal to the outer (parent) goal, "
    "naming each endpoint by its full statement. Do this even when the sub-goal is "
    "not bold.\n"
    "- advances_goal: a Department or Initiative advances the goals listed for it. "
    'When the Section breadcrumb ends in "Advances goals" (e.g. Section '
    '"Engineering > Advances goals"), the owner is the unit named at the START of '
    "that breadcrumb (here the Department Engineering); emit a mention for that "
    "owner (use its name from the Section even though it may not appear in the "
    "chunk body) and an advances_goal edge from the owner to EACH goal listed in "
    "the chunk, naming the goal endpoint by its full statement.\n"
    "- Emit a relation only when the chunk states or strongly implies it. Give the "
    "source and destination as the surface forms of the two entities, and pick the "
    "ontology relation whose direction matches (see the glosses).\n"
    "- Do not emit relations you are merely guessing; prefer precision over recall.\n"
    "- If the chunk states no entities or relations, return empty lists."
)


def build_extraction_prompt(chunk: Chunk) -> Prompt:
    """Assemble the extraction :class:`~enterprise_sim.core.llm.Prompt` for ``chunk``.

    The ontology-bearing system prompt is the stable, cacheable prefix (identical
    for every chunk, so prompt caching pays off across a run); the volatile suffix
    is just this chunk's locator and text.
    """
    section = chunk.section or "(document preamble)"
    brief = f"Source: {chunk.source_path}\nSection: {section}\nChunk text:\n{chunk.text}"
    return assemble_prompt(system=_SYSTEM_PROMPT, brief=brief)


def parse_extraction(chunk: Chunk, envelope: Mapping[str, Any]) -> Extraction:
    """Parse a raw ``generate_structured`` envelope into a validated :class:`Extraction`.

    Pure function of ``(chunk, envelope)`` — no backend involved. It drops any
    mention type or relation label outside the ontology, locates each surviving
    mention's span in ``chunk.text`` (unfound ⇒ ``start == end == -1``), clamps
    triple confidence to ``[0, 1]``, and removes exact-duplicate mentions/triples
    while preserving model order.
    """
    mentions = _parse_mentions(chunk, envelope.get("mentions"))
    triples = _parse_triples(chunk, envelope.get("triples"))
    return Extraction(chunk_id=chunk.id, mentions=mentions, triples=triples)


def _parse_mentions(chunk: Chunk, raw: Any) -> tuple[MentionSpan, ...]:
    """Build validated, deduped :class:`MentionSpan`\\ s from the envelope's mentions."""
    if not isinstance(raw, list):
        return ()
    mentions: list[MentionSpan] = []
    seen: set[tuple[str, str, int, int]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        surface_form = item.get("surface_form")
        type_ = item.get("type")
        if not isinstance(surface_form, str) or not surface_form:
            continue
        if type_ not in NODE_TYPES:
            continue
        start, end = _locate(chunk.text, surface_form)
        key = (surface_form, type_, start, end)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(
            MentionSpan(
                chunk_id=chunk.id,
                surface_form=surface_form,
                start=start,
                end=end,
                entity_type=type_,
                entity_id=None,
            )
        )
    return tuple(mentions)


def _parse_triples(chunk: Chunk, raw: Any) -> tuple[CandidateTriple, ...]:
    """Build validated, deduped :class:`CandidateTriple`\\ s from the envelope's triples."""
    if not isinstance(raw, list):
        return ()
    triples: list[CandidateTriple] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        src = item.get("src")
        rel = item.get("rel")
        dst = item.get("dst")
        if not isinstance(src, str) or not src or not isinstance(dst, str) or not dst:
            continue
        if rel not in RELATION_TYPES:
            continue
        key = (src, rel, dst)
        if key in seen:
            continue
        seen.add(key)
        triples.append(
            CandidateTriple(
                src_mention=src,
                rel=rel,
                dst_mention=dst,
                provenance=chunk.id,
                confidence=_clamp_confidence(item.get("confidence")),
            )
        )
    return tuple(triples)


def _locate(text: str, surface_form: str) -> tuple[int, int]:
    """Return the ``(start, end)`` of the first ``surface_form`` in ``text``.

    Returns ``(-1, -1)`` when the surface form is not a substring of the chunk —
    the model named an entity it did not quote verbatim — so the mention is kept
    but flagged as unlocated rather than given a fabricated offset.
    """
    start = text.find(surface_form)
    if start < 0:
        return _UNLOCATED, _UNLOCATED
    return start, start + len(surface_form)


def _clamp_confidence(value: Any) -> float:
    """Coerce a model-supplied confidence to a float in ``[0, 1]`` (default ``1.0``)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 1.0
    return max(0.0, min(1.0, float(value)))


def extract_chunk(
    chunk: Chunk,
    client: LLMClient,
    *,
    model: str | None = None,
) -> Extraction:
    """Extract typed mentions + candidate triples from one ``chunk`` via ``client``.

    Assembles the ontology-constrained prompt, forces the schema-shaped envelope
    out of ``client``'s backend, and parses it into a validated :class:`Extraction`.
    The backend is the caller's choice: a ``fake`` client extracts deterministically
    with no key (tests/CI); an ``anthropic_api`` client with :data:`HAIKU_MODEL`
    extracts from real text. ``model`` overrides the client's configured model
    (e.g. pinning Haiku for a client whose default is a larger model).
    """
    prompt = build_extraction_prompt(chunk)
    result = client.generate_structured(prompt, EXTRACTION_SCHEMA, model=model)
    return parse_extraction(chunk, result.data)


def extract_chunks(
    chunks: Sequence[Chunk],
    client: LLMClient,
    *,
    model: str | None = None,
) -> list[Extraction]:
    """Extract from every chunk, one :class:`Extraction` per input chunk (in order).

    Runs the per-chunk calls through the client's bounded thread pool
    (:meth:`~enterprise_sim.core.llm.LLMClient.generate_many`), so the on-disk
    response cache and cost ceiling are shared across the batch. Results are
    returned in ``chunks`` order.
    """
    tasks = [lambda c, chunk=chunk: extract_chunk(chunk, c, model=model) for chunk in chunks]
    return client.generate_many(tasks)
