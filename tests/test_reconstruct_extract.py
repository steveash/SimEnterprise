"""Schema-guided extraction tests (esim-nc6.3): keyless structure + gated LLM.

Covers the reconstruct extraction stage — one :class:`Chunk` → typed mentions +
candidate triples, constrained to the ontology — along three axes:

* **Ontology lock** — the extraction vocabulary is a subset of the gold KG's node
  / edge type constants, so a rename on the gold side fails here loudly.
* **Keyless structure** — the deterministic ``fake`` backend and a canned-envelope
  stub backend exercise prompt assembly, the forced schema, span location, and
  vocabulary validation with no key and no network (the core of the acceptance
  criteria); :func:`parse_extraction` is also tested directly as a pure function.
* **Gated real extraction** — a single case runs Haiku over a real golden-run
  chunk, skipped cleanly without ``ANTHROPIC_API_KEY`` so keyless CI stays green.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from enterprise_sim.core.llm import (
    Completion,
    LLMClient,
    LLMConfig,
    Prompt,
    TokenUsage,
    build_client,
)

# The gold node/edge type constants the extraction vocabulary must mirror.
from enterprise_sim.producers.pptx import (
    E_AUTHORED,
    E_REFERENCES,
    E_REVIEWED,
    N_ARTIFACT,
)
from enterprise_sim.reconstruct import (
    EXTRACTION_SCHEMA,
    HAIKU_MODEL,
    NODE_TYPES,
    RELATION_TYPES,
    Chunk,
    Extraction,
    build_extraction_prompt,
    extract_chunk,
    extract_chunks,
    parse_extraction,
)
from enterprise_sim.reconstruct.ontology import describe_ontology
from enterprise_sim.world_builders.builder import (
    E_ADVANCES_GOAL,
    E_LEADS,
    E_MEMBER_OF,
    E_OWNS,
    E_PART_OF,
    E_REPORTS_TO,
    E_SUBGOAL_OF,
    N_COMPANY,
    N_DEPARTMENT,
    N_GOAL,
    N_INITIATIVE,
    N_PERSON,
    N_PROJECT,
    N_TEAM,
)


def _chunk(text: str, *, section: str | None = "Engineering > Platform") -> Chunk:
    """A :class:`Chunk` with a stable id for extraction tests."""
    return Chunk(
        id="chunk-1",
        text=text,
        source_path="org/platform.md",
        offset=0,
        section=section,
    )


_ORG_TEXT = "Ada Lovelace reports to Grace Hopper. Ada is a member of the Platform team."


# ---------------------------------------------------------------------------
# Ontology lock: reconstruct vocab ⊆ gold KG vocab.
# ---------------------------------------------------------------------------


def test_node_types_mirror_gold_constants() -> None:
    gold_node_types = {
        N_COMPANY,
        N_DEPARTMENT,
        N_TEAM,
        N_PERSON,
        N_GOAL,
        N_INITIATIVE,
        N_PROJECT,
        N_ARTIFACT,
    }
    # Every gold node-type constant is a valid extraction target (plus the
    # scheduler-derived CalendarEvent, which has no builder constant).
    assert gold_node_types <= NODE_TYPES
    assert "CalendarEvent" in NODE_TYPES


def test_relation_types_are_gold_edge_labels() -> None:
    # The text-assertable relations the extractor emits are all real gold edges.
    for gold_edge in (
        E_REPORTS_TO,
        E_MEMBER_OF,
        E_LEADS,
        E_OWNS,
        E_ADVANCES_GOAL,
        E_PART_OF,
        E_SUBGOAL_OF,
        E_AUTHORED,
        E_REVIEWED,
        E_REFERENCES,
    ):
        assert gold_edge in RELATION_TYPES
    # Mechanical / derived edges are deliberately not extracted from text.
    assert "mentions" not in RELATION_TYPES
    assert "has_calendar_event" not in RELATION_TYPES
    assert "expresses" not in RELATION_TYPES


# ---------------------------------------------------------------------------
# Schema + prompt assembly.
# ---------------------------------------------------------------------------


def test_schema_enums_match_ontology() -> None:
    mention_type = EXTRACTION_SCHEMA["properties"]["mentions"]["items"]["properties"]["type"]
    triple_rel = EXTRACTION_SCHEMA["properties"]["triples"]["items"]["properties"]["rel"]
    assert set(mention_type["enum"]) == NODE_TYPES
    assert set(triple_rel["enum"]) == RELATION_TYPES
    # Enums are sorted so the schema (and its cache key) is deterministic.
    assert mention_type["enum"] == sorted(NODE_TYPES)
    assert triple_rel["enum"] == sorted(RELATION_TYPES)


def test_prompt_carries_ontology_and_chunk() -> None:
    prompt = build_extraction_prompt(_chunk(_ORG_TEXT))
    text = prompt.text
    # Stable ontology prefix + volatile chunk suffix.
    assert describe_ontology() in text
    assert _ORG_TEXT in text
    assert "org/platform.md" in text
    assert "Engineering > Platform" in text
    # The ontology lives in the cacheable system prefix, the chunk in the suffix.
    assert describe_ontology() in prompt.system_text
    assert _ORG_TEXT in prompt.user_text


def test_prompt_labels_missing_section() -> None:
    prompt = build_extraction_prompt(_chunk("Some preamble prose.", section=None))
    assert "(document preamble)" in prompt.user_text


# ---------------------------------------------------------------------------
# parse_extraction: pure validation, span location, dedup.
# ---------------------------------------------------------------------------


def test_parse_locates_spans_and_keeps_valid_vocab() -> None:
    chunk = _chunk(_ORG_TEXT)
    envelope = {
        "mentions": [
            {"surface_form": "Ada Lovelace", "type": "Person"},
            {"surface_form": "Platform", "type": "Team"},
        ],
        "triples": [
            {"src": "Ada Lovelace", "rel": "reports_to", "dst": "Grace Hopper", "confidence": 0.9},
        ],
    }
    result = parse_extraction(chunk, envelope)
    assert isinstance(result, Extraction)
    assert result.chunk_id == chunk.id

    ada, platform = result.mentions
    assert (ada.surface_form, ada.entity_type) == ("Ada Lovelace", "Person")
    assert (platform.surface_form, platform.entity_type) == ("Platform", "Team")
    # Spans are located by substring search, not trusted from the model.
    assert chunk.text[ada.start : ada.end] == "Ada Lovelace"
    assert chunk.text[platform.start : platform.end] == "Platform"
    assert ada.chunk_id == chunk.id and ada.entity_id is None

    (triple,) = result.triples
    assert (triple.src_mention, triple.rel, triple.dst_mention) == (
        "Ada Lovelace",
        "reports_to",
        "Grace Hopper",
    )
    assert triple.provenance == chunk.id
    assert triple.confidence == 0.9


def test_parse_drops_off_vocabulary() -> None:
    chunk = _chunk(_ORG_TEXT)
    envelope = {
        "mentions": [
            {"surface_form": "Ada Lovelace", "type": "Wizard"},  # bad type
            {"surface_form": "Grace Hopper", "type": "Person"},  # ok
        ],
        "triples": [
            {"src": "Ada Lovelace", "rel": "befriends", "dst": "Grace Hopper"},  # bad rel
            {"src": "Ada Lovelace", "rel": "reports_to", "dst": "Grace Hopper"},  # ok
        ],
    }
    result = parse_extraction(chunk, envelope)
    assert [m.surface_form for m in result.mentions] == ["Grace Hopper"]
    assert [t.rel for t in result.triples] == ["reports_to"]


def test_parse_marks_unlocated_mentions() -> None:
    chunk = _chunk(_ORG_TEXT)
    envelope = {
        "mentions": [{"surface_form": "Charles Babbage", "type": "Person"}],  # absent
        "triples": [],
    }
    (mention,) = parse_extraction(chunk, envelope).mentions
    assert mention.start == -1 and mention.end == -1


def test_parse_clamps_confidence_and_defaults() -> None:
    chunk = _chunk(_ORG_TEXT)
    envelope = {
        "mentions": [],
        "triples": [
            {"src": "Ada Lovelace", "rel": "leads", "dst": "Platform", "confidence": 5.0},
            {"src": "Ada Lovelace", "rel": "member_of", "dst": "Platform", "confidence": -1.0},
            {"src": "Grace Hopper", "rel": "owns", "dst": "Platform"},  # no confidence
        ],
    }
    triples = parse_extraction(chunk, envelope).triples
    assert [t.confidence for t in triples] == [1.0, 0.0, 1.0]


def test_parse_dedups_exact_duplicates() -> None:
    chunk = _chunk(_ORG_TEXT)
    envelope = {
        "mentions": [
            {"surface_form": "Ada Lovelace", "type": "Person"},
            {"surface_form": "Ada Lovelace", "type": "Person"},  # exact dup
        ],
        "triples": [
            {"src": "Ada Lovelace", "rel": "reports_to", "dst": "Grace Hopper"},
            {"src": "Ada Lovelace", "rel": "reports_to", "dst": "Grace Hopper"},  # dup
        ],
    }
    result = parse_extraction(chunk, envelope)
    assert len(result.mentions) == 1
    assert len(result.triples) == 1


def test_parse_tolerates_malformed_envelope() -> None:
    chunk = _chunk(_ORG_TEXT)
    envelope = {
        "mentions": [
            "not-a-dict",
            {"surface_form": "", "type": "Person"},  # empty surface form
            {"type": "Person"},  # missing surface form
            {"surface_form": "Ada Lovelace"},  # missing type
        ],
        "triples": [
            {"src": "Ada Lovelace", "rel": "reports_to"},  # missing dst
            {"src": "", "rel": "reports_to", "dst": "Grace Hopper"},  # empty src
        ],
    }
    result = parse_extraction(chunk, envelope)
    assert result.mentions == ()
    assert result.triples == ()


def test_parse_handles_missing_keys() -> None:
    result = parse_extraction(_chunk(_ORG_TEXT), {})
    assert result.mentions == ()
    assert result.triples == ()


# ---------------------------------------------------------------------------
# Keyless extract_chunk via the fake backend (schema-shaped, no key).
# ---------------------------------------------------------------------------


def test_extract_chunk_fake_backend_is_valid_and_deterministic() -> None:
    client = build_client(LLMConfig(backend="fake"))
    chunk = _chunk(_ORG_TEXT)
    result = extract_chunk(chunk, client)

    # The fake backend synthesizes one schema-shaped mention + triple; the enum
    # fields guarantee valid ontology vocabulary.
    assert result.chunk_id == chunk.id
    assert len(result.mentions) == 1
    assert len(result.triples) == 1
    (mention,) = result.mentions
    (triple,) = result.triples
    assert mention.entity_type in NODE_TYPES
    assert triple.rel in RELATION_TYPES
    assert triple.provenance == chunk.id
    assert 0.0 <= triple.confidence <= 1.0

    # Deterministic: identical inputs → identical extraction.
    again = extract_chunk(chunk, build_client(LLMConfig(backend="fake")))
    assert again == result


# ---------------------------------------------------------------------------
# Keyless extract_chunk via a canned-envelope stub backend (realistic offsets).
# ---------------------------------------------------------------------------


class _CannedBackend:
    """A backend that returns a fixed extraction envelope (no schema synthesis)."""

    name = "canned"

    def __init__(self, envelope: Mapping[str, Any]) -> None:
        self._envelope = dict(envelope)

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        return Completion(
            text="",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            model=model,
            structured=self._envelope,
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:  # pragma: no cover - unused by extraction
        raise NotImplementedError


def test_extract_chunk_canned_backend_locates_and_filters() -> None:
    envelope = {
        "mentions": [
            {"surface_form": "Ada Lovelace", "type": "Person"},
            {"surface_form": "Platform", "type": "Team"},
            {"surface_form": "Ada Lovelace", "type": "Sorcerer"},  # off-vocab type dropped
        ],
        "triples": [
            {"src": "Ada Lovelace", "rel": "member_of", "dst": "Platform", "confidence": 0.8},
            {"src": "Ada Lovelace", "rel": "casts_spell", "dst": "Platform"},  # off-vocab dropped
        ],
    }
    chunk = _chunk(_ORG_TEXT)
    client = LLMClient(_CannedBackend(envelope), config=LLMConfig(backend="canned"))
    result = extract_chunk(chunk, client)

    assert [m.surface_form for m in result.mentions] == ["Ada Lovelace", "Platform"]
    ada = result.mentions[0]
    assert chunk.text[ada.start : ada.end] == "Ada Lovelace"
    assert [t.rel for t in result.triples] == ["member_of"]
    assert result.triples[0].confidence == 0.8


def test_extract_chunks_preserves_order() -> None:
    envelope: dict[str, Any] = {"mentions": [], "triples": []}
    client = LLMClient(_CannedBackend(envelope), config=LLMConfig(backend="canned"))
    chunks = [
        Chunk(id=f"c{i}", text="text", source_path="a.md", offset=0, section=None) for i in range(3)
    ]
    results = extract_chunks(chunks, client)
    assert [r.chunk_id for r in results] == ["c0", "c1", "c2"]


# ---------------------------------------------------------------------------
# Gated real extraction (Haiku): skipped without a key.
# ---------------------------------------------------------------------------

requires_key = pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic")),
    reason="real extraction needs ANTHROPIC_API_KEY + the anthropic SDK (gated; esim-nc6.3)",
)


@requires_key
def test_extract_chunk_with_haiku() -> None:  # pragma: no cover - needs a key + network
    client = build_client(LLMConfig(backend="anthropic_api", model=HAIKU_MODEL))
    chunk = _chunk(_ORG_TEXT)
    result = extract_chunk(chunk, client, model=HAIKU_MODEL)
    # Real extraction should recover the people and the reporting relation; assert
    # only the structural invariants so the gated test is not flaky on wording.
    assert all(m.entity_type in NODE_TYPES for m in result.mentions)
    assert all(t.rel in RELATION_TYPES for t in result.triples)
    assert all(t.provenance == chunk.id for t in result.triples)
    assert result.mentions or result.triples
