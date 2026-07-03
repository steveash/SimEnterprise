"""Entity-resolution tests (esim-nc6.4): keyless cascade + gated adjudication.

Covers the reconstruct resolution stage — typed mentions → canonical nodes — along
the axes the acceptance criteria name:

* **Blocking + identical-name merge (tier 1)** — mentions are compared only within
  a type, and an identical normalized surface form merges outright.
* **Hybrid similarity (tier 2), keyless** — char-n-gram TF-IDF over names plus
  TF-IDF over context merges same-context short forms (``Alice Wong`` / ``Alice``)
  with no key, while keeping distinct entities apart; the name term caps a
  context-only coincidence below the merge bar.
* **Over-/under-merge is measurable** — resolution feeds the fidelity scorer
  (esim-nc6.6), whose :class:`EntityResolution` counts respond to a deliberate
  under-merge.
* **Canonical nodes** — best label, alias set, deterministic ids (order-independent,
  de-duplicated), gold-schema :class:`~enterprise_sim.core.world.Node` output.
* **Gated LLM adjudication (tier 3)** — the ambiguous band is wired to a client;
  a canned backend exercises it keyless, and the real Haiku path is skipped
  cleanly without ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from enterprise_sim.core.llm import (
    Completion,
    LLMClient,
    LLMConfig,
    Prompt,
    TokenUsage,
)
from enterprise_sim.core.world import Node, World
from enterprise_sim.reconstruct import (
    ADJUDICATION_SCHEMA,
    Chunk,
    MentionSpan,
    ReconstructedKG,
    Resolution,
    adjudicate_pair,
    build_adjudication_prompt,
    resolve_entities,
    score_fidelity,
)
from enterprise_sim.reconstruct.extract import HAIKU_MODEL

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _mentions(chunk: Chunk, forms: Sequence[tuple[str, str]]) -> list[MentionSpan]:
    """Locate each ``(surface_form, type)`` in ``chunk`` and build a mention.

    Uses ``str.find`` for the span, matching how the extractor locates mentions;
    a form absent from the chunk is left unlocated (``-1``).
    """
    spans = []
    for surface_form, type_ in forms:
        start = chunk.text.find(surface_form)
        end = start + len(surface_form) if start >= 0 else -1
        spans.append(
            MentionSpan(
                chunk_id=chunk.id,
                surface_form=surface_form,
                start=start,
                end=end,
                entity_type=type_,
            )
        )
    return spans


# ---------------------------------------------------------------------------
# Tier 1: blocking by type + identical-name merge.
# ---------------------------------------------------------------------------


def test_blocks_by_type_never_crosses() -> None:
    chunk = Chunk(id="c1", text="Platform ships. Platform is a division.", source_path="a.md")
    mentions = _mentions(chunk, [("Platform", "Team"), ("Platform", "Department")])
    res = resolve_entities(mentions, [chunk])
    # Same surface form, different type ⇒ two entities, never merged.
    assert {e.type for e in res.entities} == {"Team", "Department"}
    assert len(res.entities) == 2


def test_identical_normalized_name_merges_without_similarity() -> None:
    # Case/whitespace differences normalize away; both are the trivial duplicate.
    chunk = Chunk(id="c1", text="The  Platform team and the platform group.", source_path="a.md")
    mentions = _mentions(chunk, [("Platform", "Team")])
    mentions.append(
        MentionSpan(chunk_id="c1", surface_form="platform", start=30, end=38, entity_type="Team")
    )
    res = resolve_entities(mentions, [chunk])
    assert len(res.entities) == 1
    (entity,) = res.entities
    assert entity.aliases == ("Platform", "platform")


# ---------------------------------------------------------------------------
# Tier 2: keyless hybrid similarity.
# ---------------------------------------------------------------------------

_ALIAS_TEXT = (
    "Alice Wong manages Sales and Alice closed the quarter's largest deal. "
    "Separately, Ben Cho leads Platform; Ben reviewed the migration design."
)


def test_hybrid_merges_same_context_short_forms_keyless() -> None:
    chunk = Chunk(id="c1", text=_ALIAS_TEXT, source_path="a.md")
    mentions = _mentions(
        chunk,
        [
            ("Alice Wong", "Person"),
            ("Alice", "Person"),
            ("Ben Cho", "Person"),
            ("Ben", "Person"),
        ],
    )
    res = resolve_entities(mentions, [chunk])  # no client: deterministic path only
    by_label = {e.label: e for e in res.entities}
    # Full name + short form in the same neighborhood resolve to one entity each.
    assert set(by_label) == {"Alice Wong", "Ben Cho"}
    assert by_label["Alice Wong"].aliases == ("Alice", "Alice Wong")
    assert by_label["Ben Cho"].aliases == ("Ben", "Ben Cho")


def test_distinct_entities_stay_apart() -> None:
    chunk = Chunk(id="c1", text=_ALIAS_TEXT, source_path="a.md")
    mentions = _mentions(chunk, [("Alice Wong", "Person"), ("Ben Cho", "Person")])
    res = resolve_entities(mentions, [chunk])
    assert {e.label for e in res.entities} == {"Alice Wong", "Ben Cho"}
    ids = {res.entity_of(m) for m in mentions}
    assert len(ids) == 2  # two different canonical ids


def test_shared_context_cannot_merge_across_names() -> None:
    # Two different people named in the SAME sentence share a context window, but
    # zero name overlap caps the hybrid at context_weight (< merge_threshold).
    chunk = Chunk(
        id="c1", text="Ada Byron and Grace Hopper co-authored the report.", source_path="a.md"
    )
    mentions = _mentions(chunk, [("Ada Byron", "Person"), ("Grace Hopper", "Person")])
    res = resolve_entities(mentions, [chunk])
    assert len(res.entities) == 2


# ---------------------------------------------------------------------------
# Canonical nodes: labels, aliases, ids, gold-schema output.
# ---------------------------------------------------------------------------


def test_best_label_is_most_frequent_then_longest() -> None:
    chunk = Chunk(id="c1", text="Platform Team. Platform Team. Platform.", source_path="a.md")
    mentions = _mentions(chunk, [("Platform", "Team")])
    # "Platform Team" appears twice, "Platform" once → label is the frequent form.
    mentions += [
        MentionSpan(
            chunk_id="c1", surface_form="Platform Team", start=0, end=13, entity_type="Team"
        ),
        MentionSpan(
            chunk_id="c1", surface_form="Platform Team", start=15, end=28, entity_type="Team"
        ),
    ]
    res = resolve_entities(mentions, [chunk])
    (entity,) = res.entities
    assert entity.label == "Platform Team"
    assert entity.aliases == ("Platform", "Platform Team")


def test_canonical_node_is_gold_schema() -> None:
    chunk = Chunk(id="c1", text="Ben Cho leads Platform. Ben owns it.", source_path="a.md")
    mentions = _mentions(chunk, [("Ben Cho", "Person"), ("Ben", "Person")])
    res = resolve_entities(mentions, [chunk])
    (entity,) = res.entities
    node = entity.to_node(_NOW)
    assert isinstance(node, Node)
    assert node.id == entity.id == "person:ben-cho"
    assert node.type == "Person"
    assert node.props["name"] == "Ben Cho"
    assert node.aliases == ["Ben", "Ben Cho"]
    assert node.created_at == _NOW
    # Resolution.nodes stamps every entity with the same created_at.
    assert res.nodes(_NOW) == [node]


def test_ids_are_deterministic_and_order_independent() -> None:
    chunk = Chunk(id="c1", text=_ALIAS_TEXT, source_path="a.md")
    forms = [
        ("Alice Wong", "Person"),
        ("Ben Cho", "Person"),
        ("Alice", "Person"),
        ("Ben", "Person"),
    ]
    first = resolve_entities(_mentions(chunk, forms), [chunk])
    shuffled = resolve_entities(_mentions(chunk, list(reversed(forms))), [chunk])
    assert [e.id for e in first.entities] == [e.id for e in shuffled.entities]
    assert [e.id for e in first.entities] == ["person:alice-wong", "person:ben-cho"]


def test_colliding_slugs_get_disambiguated() -> None:
    # Two distinct same-type clusters whose labels slug to the same base id are
    # de-duplicated deterministically. (Tested on the id assigner directly: in the
    # full cascade, labels similar enough to slug-collide are also similar enough
    # to merge, so a collision only reaches here when the labels are genuinely
    # distinct — e.g. punctuation the slug strips.)
    from enterprise_sim.reconstruct.resolve import _assign_ids

    a = MentionSpan(chunk_id="c1", surface_form="Ada!", start=0, end=4, entity_type="Person")
    b = MentionSpan(chunk_id="c2", surface_form="Ada?", start=0, end=4, entity_type="Person")
    entities = _assign_ids([("Person", (a,)), ("Person", (b,))])
    assert sorted(e.id for e in entities) == ["person:ada", "person:ada-2"]


# ---------------------------------------------------------------------------
# Over-/under-merge is measurable through the fidelity scorer.
# ---------------------------------------------------------------------------


def _gold_person(node_id: str, name: str, aliases: Sequence[str]) -> Node:
    return Node(
        id=node_id, type="Person", created_at=_NOW, props={"name": name}, aliases=list(aliases)
    )


_AMBIGUOUS_C1 = Chunk(
    id="c1",
    text="Ben Cho leads the Platform migration and reviewed its design.",
    source_path="a.md",
)
_AMBIGUOUS_C2 = Chunk(
    id="c2",
    text="Ben approved the Platform migration and its design review.",
    source_path="b.md",
)


def test_under_merge_is_measurable() -> None:
    # Gold: one person known as both "Ben Cho" and "Ben". Keyless, the pair lands in
    # the ambiguous band and is left split → the scorer reports 1 under-merge. (The
    # gold id differs from the reconstructed ids, so alignment happens by name — an
    # id coincidence would let phase-1 exact match hide the ER error.)
    mentions = _mentions(_AMBIGUOUS_C1, [("Ben Cho", "Person")]) + _mentions(
        _AMBIGUOUS_C2, [("Ben", "Person")]
    )
    res = resolve_entities(mentions, [_AMBIGUOUS_C1, _AMBIGUOUS_C2])
    assert len(res.entities) == 2  # under-merged, keyless

    gold = World()
    gold.add_node(_gold_person("person:p-bencho", "Ben Cho", ["Ben Cho", "Ben"]))
    recon = ReconstructedKG(nodes=res.nodes(_NOW))
    report = score_fidelity(recon, gold)
    assert report.entity_resolution.under_merges == 1
    assert report.entity_resolution.over_merges == 0


def test_clean_resolution_scores_zero_er_errors() -> None:
    chunk = Chunk(id="c1", text=_ALIAS_TEXT, source_path="a.md")
    mentions = _mentions(
        chunk,
        [("Alice Wong", "Person"), ("Alice", "Person"), ("Ben Cho", "Person"), ("Ben", "Person")],
    )
    res = resolve_entities(mentions, [chunk])
    gold = World()
    gold.add_node(_gold_person("person:alice-wong", "Alice Wong", ["Alice Wong", "Alice"]))
    gold.add_node(_gold_person("person:ben-cho", "Ben Cho", ["Ben Cho", "Ben"]))
    report = score_fidelity(ReconstructedKG(nodes=res.nodes(_NOW)), gold)
    assert report.entity_resolution.over_merges == 0
    assert report.entity_resolution.under_merges == 0
    assert report.nodes.overall.f1 == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tier 3: adjudication schema, prompt, and the pure decision.
# ---------------------------------------------------------------------------


def test_adjudication_schema_shape() -> None:
    props = ADJUDICATION_SCHEMA["properties"]
    assert props["same_entity"]["type"] == "boolean"
    assert props["confidence"]["type"] == "number"
    assert ADJUDICATION_SCHEMA["required"] == ["same_entity"]


def test_adjudication_prompt_carries_both_mentions() -> None:
    a = MentionSpan(chunk_id="c1", surface_form="Ben Cho", start=0, end=7, entity_type="Person")
    b = MentionSpan(chunk_id="c2", surface_form="Ben", start=0, end=3, entity_type="Person")
    prompt = build_adjudication_prompt(a, b, "Ben Cho leads Platform.", "Ben reviewed it.")
    text = prompt.text
    assert "Ben Cho" in text and "Ben reviewed it." in text and "Person" in text
    # Stable rules live in the cacheable system prefix; the pair in the suffix.
    assert "same_entity" in prompt.system_text
    assert "Ben Cho leads Platform." in prompt.user_text


class _CannedBackend:
    """A backend that returns a fixed adjudication envelope (no schema synthesis)."""

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
    ) -> Completion:  # pragma: no cover - unused by resolution
        raise NotImplementedError


def _canned_client(envelope: Mapping[str, Any]) -> LLMClient:
    return LLMClient(_CannedBackend(envelope), config=LLMConfig(backend="canned"))


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        ({"same_entity": True, "confidence": 0.9}, True),
        ({"same_entity": False, "confidence": 0.9}, False),
        ({"same_entity": True, "confidence": 0.2}, False),  # below min_confidence
        ({"same_entity": True}, True),  # missing confidence ⇒ treated as 1.0
    ],
)
def test_adjudicate_pair_decision(envelope: dict[str, Any], expected: bool) -> None:
    a = MentionSpan(chunk_id="c1", surface_form="Ben Cho", start=0, end=7, entity_type="Person")
    b = MentionSpan(chunk_id="c1", surface_form="Ben", start=0, end=3, entity_type="Person")
    decision = adjudicate_pair(a, b, _canned_client(envelope), context_a="x", context_b="y")
    assert decision is expected


def test_ambiguous_band_merges_only_with_a_client() -> None:
    # Two chunks whose short-form/full-name pair lands in the ambiguous band:
    # moderate context overlap (misses tier 2) with real name overlap, so it is
    # sent to the LLM. Keyless it stays split; a "same entity" client merges it.
    mentions = _mentions(_AMBIGUOUS_C1, [("Ben Cho", "Person")]) + _mentions(
        _AMBIGUOUS_C2, [("Ben", "Person")]
    )
    chunks = [_AMBIGUOUS_C1, _AMBIGUOUS_C2]

    keyless = resolve_entities(mentions, chunks)
    assert len(keyless.entities) == 2

    merged = resolve_entities(
        mentions, chunks, client=_canned_client({"same_entity": True, "confidence": 0.95})
    )
    assert len(merged.entities) == 1
    (entity,) = merged.entities
    assert entity.aliases == ("Ben", "Ben Cho")


def test_low_similarity_pairs_never_reach_the_client() -> None:
    # A client that would merge anything must NOT be consulted for a pair below the
    # ambiguous band — distinct names never even become candidates.
    chunk = Chunk(id="c1", text="Ada Byron and Grace Hopper wrote it.", source_path="a.md")
    mentions = _mentions(chunk, [("Ada Byron", "Person"), ("Grace Hopper", "Person")])
    res = resolve_entities(
        mentions, [chunk], client=_canned_client({"same_entity": True, "confidence": 1.0})
    )
    assert len(res.entities) == 2


def test_resolution_is_a_dataclass_with_full_map() -> None:
    chunk = Chunk(id="c1", text=_ALIAS_TEXT, source_path="a.md")
    mentions = _mentions(chunk, [("Alice Wong", "Person"), ("Alice", "Person")])
    res = resolve_entities(mentions, [chunk])
    assert isinstance(res, Resolution)
    # Every input mention appears in the map, pointing at a real entity id.
    ids = {e.id for e in res.entities}
    assert all(res.entity_of(m) in ids for m in mentions)


# ---------------------------------------------------------------------------
# Gated real adjudication (Haiku): skipped without a key.
# ---------------------------------------------------------------------------

requires_key = pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic")),
    reason="real adjudication needs ANTHROPIC_API_KEY + the anthropic SDK (gated; esim-nc6.4)",
)


@requires_key
def test_adjudicate_pair_with_haiku() -> None:  # pragma: no cover - needs a key + network
    from enterprise_sim.core.llm import build_client

    client = build_client(LLMConfig(backend="anthropic_api", model=HAIKU_MODEL))
    a = MentionSpan(chunk_id="c1", surface_form="Ben Cho", start=0, end=7, entity_type="Person")
    b = MentionSpan(chunk_id="c1", surface_form="Ben", start=0, end=3, entity_type="Person")
    # Same-sentence full name + short form: the model should call these one person.
    decision = adjudicate_pair(
        a,
        b,
        client,
        context_a="Ben Cho leads the Platform team; Ben reviewed the design.",
        context_b="Ben Cho leads the Platform team; Ben reviewed the design.",
        model=HAIKU_MODEL,
    )
    assert isinstance(decision, bool)
