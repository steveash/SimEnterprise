"""RAG baseline runner tests (esim-uzc.5): retrieval, id-resolution, and the runner.

Covers :mod:`enterprise_sim.benchmark.runners.rag`. The keyless half — text
extraction across media, BM25 retrieval, surface-form → node-id resolution, and
the runner driven by a deterministic stub client — is exercised here without a
key. The genuine ``anthropic_api`` path is a single gated test, skipped unless
``ANTHROPIC_API_KEY`` is set, so CI stays network-free.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from enterprise_sim.benchmark.generate import generate, load_world_from_run
from enterprise_sim.benchmark.runners.rag import (
    AliasResolver,
    BM25Index,
    Chunk,
    RagRunner,
    build_runner,
    extract_text,
    load_corpus,
    run_rag,
)
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.benchmark.score import Predictions, score
from enterprise_sim.cli import main
from enterprise_sim.core.llm import Completion, LLMClient, LLMConfig, Prompt, TokenUsage
from enterprise_sim.producers.word_docx import DocxDocument, build_docx


@pytest.fixture(scope="session")
def golden_run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real golden run on disk — corpus + answer key — built once per session.

    The committed ``runs/`` tree is gitignored, so tests cannot depend on a
    checked-in run directory. Instead, execute the deterministic ``fake``-backend
    golden run (the same fixture the generators use) into a temporary directory,
    giving a real corpus + ``aliases``/``mentions`` answer key without a network
    or a key. Session-scoped so the run executes only once.
    """
    from enterprise_sim.benchmark.fixtures import golden_run

    tmp = tmp_path_factory.mktemp("golden-run")
    return golden_run(tmp).run_dir


# --------------------------------------------------------------------------- #
# A deterministic stub client: returns a fixed answer, no key, no network.
# --------------------------------------------------------------------------- #


class _ScriptedBackend:
    """A backend that always returns the same prose answer (for resolution tests)."""

    name = "scripted"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:
        raise NotImplementedError

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        self.prompts.append(prompt.user_text)
        return Completion(text=self.answer, usage=TokenUsage(), model=model)


def _scripted_client(answer: str) -> tuple[LLMClient, _ScriptedBackend]:
    backend = _ScriptedBackend(answer)
    client = LLMClient(backend, config=LLMConfig(backend="scripted", cache_enabled=False))
    return client, backend


# --------------------------------------------------------------------------- #
# Text extraction across media.
# --------------------------------------------------------------------------- #


def test_extract_markdown_is_verbatim(tmp_path: Path) -> None:
    md = tmp_path / "note.md"
    md.write_text("# Title\n\nBen Cho leads the team.\n", encoding="utf-8")
    assert "Ben Cho leads the team." in extract_text(md)


def test_extract_docx_recovers_prose(tmp_path: Path) -> None:
    doc = DocxDocument(body=["Status Report", "Cleo Costa shipped the build."])
    path = tmp_path / "status.docx"
    path.write_bytes(build_docx(doc))

    text = extract_text(path)
    assert "Status Report" in text
    assert "Cleo Costa shipped the build." in text


def test_extract_json_flattens_string_leaves(tmp_path: Path) -> None:
    issue = {
        "key": "BSD-7206",
        "fields": {
            "summary": "Groom the backlog",
            "labels": ["backlog", "engineering"],
            "comments": [{"body": "Yuki Quintero triaged this."}],
            "votes": 3,
        },
    }
    path = tmp_path / "issue.jira.json"
    path.write_text(json.dumps(issue), encoding="utf-8")

    text = extract_text(path)
    assert "Groom the backlog" in text
    assert "Yuki Quintero triaged this." in text
    assert "backlog" in text
    # Non-string scalars are dropped, not stringified.
    assert "3" not in text


def test_extract_eml_recovers_subject_and_body(tmp_path: Path) -> None:
    path = tmp_path / "msg.eml"
    path.write_text(
        "From: a@example.com\nTo: b@example.com\nSubject: Sprint sync\n\n"
        "Tara Ibarra owns the rollout.\n",
        encoding="utf-8",
    )
    text = extract_text(path)
    assert "Sprint sync" in text
    assert "Tara Ibarra owns the rollout." in text


# --------------------------------------------------------------------------- #
# Corpus loading over the real golden run.
# --------------------------------------------------------------------------- #


def test_load_corpus_tags_chunks_with_real_node_ids(golden_run_dir: Path) -> None:
    world = load_world_from_run(golden_run_dir)
    chunks = load_corpus(golden_run_dir, world)

    assert chunks, "the golden run has artifacts to index"
    # Every chunk's artifact_id is a real Artifact node in the gold graph.
    artifact_ids = {node.id for node in world.nodes_by_type("Artifact")}
    assert {chunk.artifact_id for chunk in chunks} <= artifact_ids
    # Both markdown and docx/jira media made it into the corpus as non-empty text.
    assert all(chunk.text.strip() for chunk in chunks)
    suffixes = {Path(chunk.path).suffix for chunk in chunks}
    assert ".md" in suffixes
    assert ".docx" in suffixes or ".json" in suffixes


def test_load_corpus_is_deterministic(golden_run_dir: Path) -> None:
    world = load_world_from_run(golden_run_dir)
    first = load_corpus(golden_run_dir, world)
    second = load_corpus(golden_run_dir, world)
    assert first == second


# --------------------------------------------------------------------------- #
# BM25 retrieval.
# --------------------------------------------------------------------------- #


def _chunks(*texts: str) -> list[Chunk]:
    return [
        Chunk(artifact_id=f"art-{i}", path=f"{i}.md", index=0, text=t) for i, t in enumerate(texts)
    ]


def test_bm25_ranks_the_relevant_chunk_first() -> None:
    chunks = _chunks(
        "The quarterly budget review covers finance and accounting.",
        "Ben Cho leads the platform infrastructure migration this sprint.",
        "Lunch options near the office include sushi and salad.",
    )
    index = BM25Index.build(chunks)
    hits = index.search("who leads the infrastructure migration", k=2)

    assert hits, "a known query retrieves relevant chunks"
    assert hits[0][0] is chunks[1]
    assert hits[0][1] > 0.0


def test_bm25_returns_nothing_for_a_disjoint_query() -> None:
    index = BM25Index.build(_chunks("alpha beta gamma", "delta epsilon zeta"))
    assert index.search("xylophone quokka", k=3) == []


def test_bm25_respects_k_and_is_deterministic() -> None:
    chunks = _chunks(
        "sprint planning sprint review sprint demo",
        "sprint retrospective notes",
        "unrelated cafeteria menu",
    )
    index = BM25Index.build(chunks)
    hits = index.search("sprint", k=1)
    assert len(hits) == 1
    # Same query, same result, every time.
    assert [c.text for c, _ in index.search("sprint", k=2)] == [
        c.text for c, _ in index.search("sprint", k=2)
    ]


def test_bm25_empty_corpus_is_safe() -> None:
    index = BM25Index.build([])
    assert index.search("anything", k=3) == []


# --------------------------------------------------------------------------- #
# Surface-form → node-id resolution.
# --------------------------------------------------------------------------- #


def test_resolver_maps_surface_forms_to_ids() -> None:
    resolver = AliasResolver.of(
        {
            "Ben Cho": ["person:ben-cho"],
            "BSD-7206": ["artifact:groom:jira"],
        }
    )
    ids = resolver.resolve("The kickoff names Ben Cho; see BSD-7206 for the backlog.")
    assert ids == frozenset({"person:ben-cho", "artifact:groom:jira"})


def test_resolver_prefers_the_longest_surface_form() -> None:
    resolver = AliasResolver.of(
        {
            "Cleo": ["person:cleo-costa", "person:cleo-diaz"],
            "Cleo Costa": ["person:cleo-costa"],
        }
    )
    # The longer, unambiguous surface wins — the bare "Cleo" inside it is not also
    # matched, so the ambiguous pair is not pulled in.
    assert resolver.resolve("Cleo Costa shipped it.") == frozenset({"person:cleo-costa"})


def test_resolver_is_word_bounded() -> None:
    resolver = AliasResolver.of({"cat": ["animal:cat"]})
    assert resolver.resolve("the category was concatenated") == frozenset()
    assert resolver.resolve("the cat sat") == frozenset({"animal:cat"})


def test_resolver_returns_all_entities_for_an_ambiguous_surface() -> None:
    resolver = AliasResolver.of({"Cleo": ["person:cleo-costa", "person:cleo-diaz"]})
    assert resolver.resolve("Ask Cleo about it.") == frozenset(
        {"person:cleo-costa", "person:cleo-diaz"}
    )


def test_resolver_empty_is_safe() -> None:
    resolver = AliasResolver.of({})
    assert resolver.resolve("anything at all") == frozenset()


def test_resolver_from_run_loads_aliases_and_mentions(golden_run_dir: Path) -> None:
    resolver = AliasResolver.from_run(golden_run_dir)
    assert resolver.by_surface, "the golden run answer key has surface forms"
    # A canonical person name resolves to that person's node id.
    ids = resolver.resolve("Ben Cho attended.")
    assert any(node_id.startswith("person:") for node_id in ids)


# --------------------------------------------------------------------------- #
# The runner: retrieve → answer (stub) → resolve.
# --------------------------------------------------------------------------- #


def test_runner_resolves_the_model_answer_to_ids() -> None:
    index = BM25Index.build(_chunks("Ben Cho leads the build.", "unrelated"))
    resolver = AliasResolver.of({"Ben Cho": ["person:ben-cho"]})
    runner = RagRunner(index=index, resolver=resolver, top_k=2)
    pair = QAPair(
        id="qa-1",
        question="Who leads the build?",
        qtype="who",
        reasoning_type="direct_relation",
        expected_ids=("person:ben-cho",),
    )
    client, backend = _scripted_client("Ben Cho leads the build.")

    prediction = runner.answer(pair, client)
    assert prediction.qa_id == "qa-1"
    assert prediction.predicted_ids == ("person:ben-cho",)
    # The retrieved chunk text reached the model's prompt.
    assert "Ben Cho leads the build." in backend.prompts[0]


def test_runner_run_produces_one_prediction_per_question(golden_run_dir: Path) -> None:
    runner = build_runner(golden_run_dir)
    benchmark = generate(golden_run_dir)
    subset = Benchmark.of(list(benchmark)[:3])
    client, _ = _scripted_client("Ben Cho and Cleo Costa.")

    predictions = runner.run(subset, client)
    assert isinstance(predictions, Predictions)
    assert {p.qa_id for p in predictions} == {pair.id for pair in subset}
    # The predictions score cleanly against the benchmark (no crash, valid sets).
    report = score(subset, predictions)
    assert report.overall.count == len(subset)


def test_build_runner_indexes_the_corpus(golden_run_dir: Path) -> None:
    runner = build_runner(golden_run_dir, top_k=4)
    assert runner.top_k == 4
    assert runner.index.chunks, "the runner indexed the golden corpus"
    assert runner.resolver.by_surface


# --------------------------------------------------------------------------- #
# CLI wiring (keyless, via the fake backend).
# --------------------------------------------------------------------------- #


def test_cli_bench_run_writes_predictions(
    tmp_path: Path, capsys: Any, golden_run_dir: Path
) -> None:
    bench_path = tmp_path / "bench.jsonl"
    generate(golden_run_dir).write_jsonl(bench_path)
    out_path = tmp_path / "pred.rag.jsonl"

    code = main(
        [
            "bench",
            "run",
            "--runner",
            "rag",
            "--bench",
            str(bench_path),
            "--run",
            str(golden_run_dir),
            "-o",
            str(out_path),
            "--backend",
            "fake",
        ]
    )
    assert code == 0
    # A valid predictions file came out, one row per benchmark question.
    predictions = Predictions.read_jsonl(out_path)
    benchmark = Benchmark.read_jsonl(bench_path)
    assert len(predictions) == len(benchmark)
    err = capsys.readouterr().err
    assert "bench run --runner rag" in err


# --------------------------------------------------------------------------- #
# Gated live path: the real anthropic_api backend (needs a key).
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live RAG answer requires ANTHROPIC_API_KEY",
)
def test_live_rag_runner_scores_a_subset(
    golden_run_dir: Path,
) -> None:  # pragma: no cover - needs a key
    from enterprise_sim.core.llm import build_client

    benchmark = generate(golden_run_dir)
    subset = Benchmark.of(list(benchmark)[:3])
    client = build_client(LLMConfig(backend="anthropic_api"))

    predictions = run_rag(golden_run_dir, subset, client, top_k=5)
    assert len(predictions) == len(subset)
    report = score(subset, predictions)
    assert report.overall.count == len(subset)
