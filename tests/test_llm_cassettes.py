"""Cassette record/replay self-test + scenario skip semantics (spec 0002 §1, slice 2).

**This is not a transport test.** A cassette is the existing on-disk
:class:`~enterprise_sim.core.llm.cache.ResponseCache` pointed at a committed directory:
a warm hit replays a recorded :class:`~enterprise_sim.core.llm.types.Completion` through
the production cache path, so replay exercises *response content* flowing through the
parsing/resolution pipeline (D31), **not** the SDK wire format — that is covered by
``tests/test_llm_sdk_path.py`` plus the import-smoke signature pin.

The self-tests here prove the infrastructure keyless and offline, using the deterministic
``fake`` backend as the recording "provider":

* record → replay round-trips byte-stably, and replay never re-writes the cassette;
* a drifted prompt misses the cache and raises a terminal
  :class:`~enterprise_sim.core.llm.types.LLMError` naming the re-record command
  (:class:`~tests.llm_stubs.CassetteMissBackend`);
* :func:`~tests.llm_stubs.require_cassette` skips (does not fail) when a scenario has no
  recording, and record mode without a key skips with a clear message;
* the record-time redaction scan flags a credential-shaped secret.

The frozen extract/resolve scenario replays landed in slice 3; the RAG mini-corpus
scenario (``RagRunner.answer`` over a literal corpus, retrieve → answer → resolve) lands
here in slice 4. Every scenario uses **frozen-literal** inputs (never the golden-run
fixture), so a golden-pin change can neither read nor invalidate a cassette. Until an
owner records them (keyed), the scenario tests skip via
:func:`~tests.llm_stubs.require_cassette`; once ``tests/cassettes/{extract,resolve,rag}/``
are committed they replay strictly and offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.runners.rag import AliasResolver, BM25Index, RagRunner
from enterprise_sim.benchmark.runners.rag import Chunk as RagChunk
from enterprise_sim.benchmark.schema import QAPair
from enterprise_sim.core.llm.client import LLMConfig, build_client
from enterprise_sim.core.llm.prompt import Prompt, PromptLayer
from enterprise_sim.core.llm.types import LLMError
from enterprise_sim.reconstruct import (
    NODE_TYPES,
    RELATION_TYPES,
    Chunk,
    Extraction,
    MentionSpan,
    adjudicate_pair,
    extract_chunk,
)
from enterprise_sim.reconstruct.extract import HAIKU_MODEL

from tests.llm_stubs import (
    CASSETTE_ROOT,
    RECORD_COMMAND,
    CassetteMissBackend,
    cassette_client,
    recording_enabled,
    replay_client,
    require_cassette,
    scan_cassette_for_secrets,
    simple_prompt,
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"kind": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["kind", "n"],
}
_CANDIDATES = ["doc-1", "doc-2", "doc-3"]


def _fake_recorder(cassette_dir: Path) -> Any:
    """A ``fake``-backed client that records into ``cassette_dir`` (the self-test provider)."""
    return build_client(LLMConfig(backend="fake", model=HAIKU_MODEL, cache_dir=str(cassette_dir)))


# ---------------------------------------------------------------------------
# Round-trip: record (fake) → replay (CassetteMissBackend), byte-stable
# ---------------------------------------------------------------------------


def test_structured_round_trip(tmp_path: Path) -> None:
    prompt = simple_prompt()
    recorded = _fake_recorder(tmp_path).generate_structured(prompt, _SCHEMA)
    assert recorded.cache_hit is False

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, "recording writes exactly one cassette file per request"
    on_disk = files[0].read_text()

    first = replay_client(tmp_path).generate_structured(prompt, _SCHEMA)
    assert first.cache_hit is True  # served from the cassette, backend never reached
    assert (first.data, first.model, first.usage) == (recorded.data, recorded.model, recorded.usage)

    # D31: replay twice → identical results, and the file is never re-written on a hit.
    second = replay_client(tmp_path).generate_structured(prompt, _SCHEMA)
    assert (second.data, second.model, second.usage, second.cache_hit) == (
        first.data,
        first.model,
        first.usage,
        first.cache_hit,
    )
    assert files[0].read_text() == on_disk


def test_content_round_trip(tmp_path: Path) -> None:
    prompt = simple_prompt()
    recorded = _fake_recorder(tmp_path).generate_content(prompt, candidate_references=_CANDIDATES)
    assert recorded.cache_hit is False

    replayed = replay_client(tmp_path).generate_content(prompt, candidate_references=_CANDIDATES)
    assert replayed.cache_hit is True
    assert (replayed.content, replayed.references_used, replayed.model) == (
        recorded.content,
        recorded.references_used,
        recorded.model,
    )


def test_cassette_client_replays_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dispatcher returns a strict replay client when not in record mode.
    monkeypatch.delenv("ESIM_CASSETTES", raising=False)
    prompt = simple_prompt()
    recorded = _fake_recorder(tmp_path).generate_structured(prompt, _SCHEMA)

    replayed = cassette_client(tmp_path).generate_structured(prompt, _SCHEMA)
    assert replayed.cache_hit is True
    assert replayed.data == recorded.data


# ---------------------------------------------------------------------------
# Drift: a mutated prompt misses and raises through CassetteMissBackend
# ---------------------------------------------------------------------------


def test_structured_drift_raises_miss(tmp_path: Path) -> None:
    prompt = simple_prompt()
    _fake_recorder(tmp_path).generate_structured(prompt, _SCHEMA)

    mutated = Prompt(
        layers=(
            PromptLayer(role="system", text="S", cacheable=True, label="system"),
            PromptLayer(role="user", text="MUTATED — this prompt was never recorded"),
        )
    )
    with pytest.raises(LLMError) as excinfo:
        replay_client(tmp_path).generate_structured(mutated, _SCHEMA)
    message = str(excinfo.value)
    assert "cassette miss" in message
    assert RECORD_COMMAND in message


def test_content_drift_raises_miss(tmp_path: Path) -> None:
    prompt = simple_prompt()
    _fake_recorder(tmp_path).generate_content(prompt, candidate_references=_CANDIDATES)

    with pytest.raises(LLMError) as excinfo:
        # A different candidate set changes the request key → miss.
        replay_client(tmp_path).generate_content(prompt, candidate_references=["other"])
    assert RECORD_COMMAND in str(excinfo.value)


def test_cassette_miss_backend_is_terminal() -> None:
    # A bare miss is a terminal LLMError (not TransientLLMError) so the client's retry
    # loop does not spin on a permanent cassette gap.
    from enterprise_sim.core.llm.types import TransientLLMError

    with pytest.raises(LLMError) as excinfo:
        CassetteMissBackend().generate_structured(
            simple_prompt(), schema=_SCHEMA, model=HAIKU_MODEL, temperature=0.0
        )
    assert not isinstance(excinfo.value, TransientLLMError)


# ---------------------------------------------------------------------------
# Skip-if-unrecorded semantics
# ---------------------------------------------------------------------------


def test_require_cassette_skips_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin replay mode: these assert the *keyless* semantics, which an ambient
    # ESIM_CASSETTES=record (e.g. during a recording session) would invert.
    monkeypatch.delenv("ESIM_CASSETTES", raising=False)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_cassette(tmp_path / "never-recorded")
    assert RECORD_COMMAND in str(excinfo.value)


def test_require_cassette_skips_when_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESIM_CASSETTES", raising=False)
    empty = tmp_path / "scenario"
    empty.mkdir()
    with pytest.raises(pytest.skip.Exception):
        require_cassette(empty)


def test_require_cassette_passes_when_recorded(tmp_path: Path) -> None:
    (tmp_path / "abc.json").write_text("{}")
    require_cassette(tmp_path)  # a populated directory does not skip


def test_require_cassette_does_not_skip_in_record_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Record mode is about to (re)create the directory, so an absent cassette is fine.
    monkeypatch.setenv("ESIM_CASSETTES", "record")
    require_cassette(tmp_path / "missing")


# ---------------------------------------------------------------------------
# Record mode without a key: skips, does not error
# ---------------------------------------------------------------------------


def test_record_mode_without_key_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESIM_CASSETTES", "record")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert recording_enabled() is True
    with pytest.raises(pytest.skip.Exception) as excinfo:
        cassette_client(tmp_path)
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "bench" in message


# ---------------------------------------------------------------------------
# Redaction scan (record-time belt-and-braces)
# ---------------------------------------------------------------------------


def test_redaction_scan_flags_sk_ant_prefix(tmp_path: Path) -> None:
    (tmp_path / "leak.json").write_text('{"text": "oops sk-ant-api03-abcdef embedded"}')
    with pytest.raises(LLMError) as excinfo:
        scan_cassette_for_secrets(tmp_path)
    assert "leak.json" in str(excinfo.value)


def test_redaction_scan_flags_live_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-key-value")
    (tmp_path / "leak.json").write_text('{"text": "super-secret-key-value"}')
    with pytest.raises(LLMError):
        scan_cassette_for_secrets(tmp_path)


def test_redaction_scan_passes_clean_cassette(tmp_path: Path) -> None:
    (tmp_path / "clean.json").write_text('{"text": "an ordinary recorded completion"}')
    scan_cassette_for_secrets(tmp_path)  # no raise


# ---------------------------------------------------------------------------
# Frozen scenario replays (spec 0002 §1, slice 3): extract_chunk / adjudicate_pair
# ---------------------------------------------------------------------------
#
# The scenario inputs below are **frozen literals**, deliberately *not* derived from the
# golden-run fixture: a cassette's ``request_key`` folds the prompt text, so binding a
# scenario to the golden pin would make every pin regeneration a keyed re-record. Each
# replay drives the *real* extract/resolve code path (``extract_chunk`` /
# ``adjudicate_pair``) through :func:`~tests.llm_stubs.cassette_client` — strict replay
# keyless (a miss fails via :class:`~tests.llm_stubs.CassetteMissBackend`), keyed
# recording under ``ESIM_CASSETTES=record``. Assertions are the structural invariants any
# competent model satisfies on these tiny fixtures plus the obviously-correct answer,
# matching the gated keyed tests' style (``test_reconstruct_extract.py`` /
# ``test_reconstruct_resolve.py``); a re-record may legitimately loosen a content check.

_EXTRACT_DIR = CASSETTE_ROOT / "extract"
_RESOLVE_DIR = CASSETTE_ROOT / "resolve"

# One chunk of enterprise org prose: two people, a team, and reporting / membership
# relations a competent model recovers as ontology-typed mentions and triples.
_EXTRACT_CHUNK = Chunk(
    id="cassette-extract-1",
    text=(
        "Priya Nair leads the Payments team. Priya reports to Dana Okoro, the head of "
        "Engineering. Marcus Bell is a member of the Payments team."
    ),
    source_path="org/payments.md",
    offset=0,
    section="Engineering > Payments",
)


def test_extract_chunk_replay() -> None:
    """``extract_chunk`` over a frozen org chunk replays ontology-valid, located output."""
    require_cassette(_EXTRACT_DIR)
    client = cassette_client(_EXTRACT_DIR)

    result = extract_chunk(_EXTRACT_CHUNK, client, model=HAIKU_MODEL)

    assert isinstance(result, Extraction)
    assert result.chunk_id == _EXTRACT_CHUNK.id
    # Closed extraction: every mention/relation is gold ontology vocabulary.
    assert all(m.entity_type in NODE_TYPES for m in result.mentions)
    assert all(t.rel in RELATION_TYPES for t in result.triples)
    # Triples carry the chunk id as provenance; located spans point back verbatim.
    assert all(t.provenance == _EXTRACT_CHUNK.id for t in result.triples)
    for mention in result.mentions:
        if mention.start >= 0:
            assert _EXTRACT_CHUNK.text[mention.start : mention.end] == mention.surface_form
    # This chunk plainly names entities and relations, so something is recovered.
    assert result.mentions or result.triples

    if recording_enabled():
        scan_cassette_for_secrets(_EXTRACT_DIR)


# A same-entity coreference pair (full name / short form) sharing one context sentence —
# the obviously-mergeable case — and a distinct-people pair that must stay split.
_COREF_A = MentionSpan(chunk_id="c1", surface_form="Ben Cho", start=0, end=7, entity_type="Person")
_COREF_B = MentionSpan(chunk_id="c1", surface_form="Ben", start=0, end=3, entity_type="Person")
_COREF_CONTEXT = "Ben Cho leads the Platform team; later that week Ben reviewed the design."

_DISTINCT_A = MentionSpan(
    chunk_id="c1", surface_form="Ben Cho", start=0, end=7, entity_type="Person"
)
_DISTINCT_B = MentionSpan(
    chunk_id="c2", surface_form="Ben Ortiz", start=0, end=9, entity_type="Person"
)
_DISTINCT_CONTEXT_A = "Ben Cho leads the Platform team in the Engineering department."
_DISTINCT_CONTEXT_B = "Ben Ortiz manages the Warehouse team in the Logistics department."


def test_adjudicate_pair_coref_replay() -> None:
    """``adjudicate_pair`` merges a full-name / short-form pair in shared context."""
    require_cassette(_RESOLVE_DIR)
    client = cassette_client(_RESOLVE_DIR)

    decision = adjudicate_pair(
        _COREF_A,
        _COREF_B,
        client,
        context_a=_COREF_CONTEXT,
        context_b=_COREF_CONTEXT,
        model=HAIKU_MODEL,
    )

    assert decision is True

    if recording_enabled():
        scan_cassette_for_secrets(_RESOLVE_DIR)


def test_adjudicate_pair_distinct_replay() -> None:
    """``adjudicate_pair`` keeps two different people with a shared first name apart."""
    require_cassette(_RESOLVE_DIR)
    client = cassette_client(_RESOLVE_DIR)

    decision = adjudicate_pair(
        _DISTINCT_A,
        _DISTINCT_B,
        client,
        context_a=_DISTINCT_CONTEXT_A,
        context_b=_DISTINCT_CONTEXT_B,
        model=HAIKU_MODEL,
    )

    assert decision is False

    if recording_enabled():
        scan_cassette_for_secrets(_RESOLVE_DIR)


# ---------------------------------------------------------------------------
# Frozen mini-corpus RAG replay (spec 0002 §1, slice 4): RagRunner.answer
# ---------------------------------------------------------------------------
#
# The RAG scenario builds a :class:`~enterprise_sim.benchmark.runners.rag.RagRunner`
# directly from a handful of literal :class:`RagChunk`\\ s (``BM25Index.build``) and a
# literal alias map (``AliasResolver.of``) — *not* the golden-run fixture
# (``test_benchmark_rag.py``'s ``golden_run_dir``). This is the load-bearing decoupling
# (spec 0002 §1): a golden-pin regeneration can neither read nor invalidate the RAG
# cassette, so a keyless contributor is never blocked behind a keyed re-record. Only the
# middle stage (the answer step) touches the model; retrieval and id-resolution are pure.
# The corpus is deliberately tiny but *retrieval-meaningful* — four artifact chunks across
# distinct teams/topics so BM25 must discriminate the Payments charter from the logistics,
# cafeteria, and goals chunks — so replay exercises the whole retrieve → answer → resolve
# path end-to-end, strict keyless (a miss fails via :class:`CassetteMissBackend`) and keyed
# under ``ESIM_CASSETTES=record``.

_RAG_DIR = CASSETTE_ROOT / "rag"

_RAG_CHUNKS = (
    RagChunk(
        artifact_id="artifact:payments-charter",
        path="org/payments-charter.md",
        index=0,
        text=(
            "The Payments team is led by Priya Nair. "
            "Priya Nair reports to Dana Okoro, the head of Engineering."
        ),
    ),
    RagChunk(
        artifact_id="artifact:logistics-charter",
        path="org/logistics-charter.md",
        index=0,
        text="The Warehouse team is led by Marcus Bell in the Logistics department.",
    ),
    RagChunk(
        artifact_id="artifact:cafeteria-menu",
        path="ops/cafeteria-menu.md",
        index=0,
        text="The cafeteria near the main office serves sushi and salad on weekdays.",
    ),
    RagChunk(
        artifact_id="artifact:eng-goals",
        path="goals/engineering.md",
        index=0,
        text="Engineering's top goal this quarter is to cut payment latency in half.",
    ),
)

# The literal answer key: surface form → KG node id, standing in for a run's
# aliases.jsonl / mentions.jsonl (``AliasResolver.from_run``) so the RAG answer scores on
# the same node-id basis as the graph runner.
_RAG_ALIASES = {
    "Priya Nair": ["person:priya-nair"],
    "Dana Okoro": ["person:dana-okoro"],
    "Marcus Bell": ["person:marcus-bell"],
}

_RAG_QUESTION = QAPair(
    id="cassette-rag-1",
    question="Who leads the Payments team?",
    qtype="who",
    reasoning_type="direct_relation",
    expected_ids=("person:priya-nair",),
)


def test_rag_runner_answer_replay() -> None:
    """``RagRunner.answer`` over a frozen mini-corpus replays retrieve → answer → resolve."""
    require_cassette(_RAG_DIR)
    client = cassette_client(_RAG_DIR)

    runner = RagRunner(
        index=BM25Index.build(_RAG_CHUNKS),
        resolver=AliasResolver.of(_RAG_ALIASES),
        top_k=3,
    )
    prediction = runner.answer(_RAG_QUESTION, client, model=HAIKU_MODEL)

    assert prediction.qa_id == _RAG_QUESTION.id
    # Resolution yields a stable, sorted tuple of *known* KG ids and nothing invented —
    # the model can only be attributed to surface forms in the frozen alias map.
    assert isinstance(prediction.predicted_ids, tuple)
    assert list(prediction.predicted_ids) == sorted(prediction.predicted_ids)
    assert set(prediction.predicted_ids) <= {
        "person:priya-nair",
        "person:dana-okoro",
        "person:marcus-bell",
    }
    # The obviously-correct answer: retrieval surfaced the Payments charter, the model
    # named its lead, and resolution mapped that name back to the person node.
    assert "person:priya-nair" in prediction.predicted_ids

    if recording_enabled():
        scan_cassette_for_secrets(_RAG_DIR)
