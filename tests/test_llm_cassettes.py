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

The keyed scenario replays (extract/resolve/rag) land in slices 3–4; until their
cassettes are recorded they skip via :func:`~tests.llm_stubs.require_cassette`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.core.llm.client import LLMConfig, build_client
from enterprise_sim.core.llm.prompt import Prompt, PromptLayer
from enterprise_sim.core.llm.types import LLMError
from enterprise_sim.reconstruct.extract import HAIKU_MODEL

from tests.llm_stubs import (
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


def test_require_cassette_skips_when_absent(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_cassette(tmp_path / "never-recorded")
    assert RECORD_COMMAND in str(excinfo.value)


def test_require_cassette_skips_when_empty(tmp_path: Path) -> None:
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
