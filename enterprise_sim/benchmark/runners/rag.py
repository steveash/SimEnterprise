"""RAG baseline runner: answer the benchmark from the raw corpus, not the graph (esim-uzc.5).

The graph runner answers each :class:`~enterprise_sim.benchmark.schema.QAPair` by
reasoning over the gold knowledge graph. This is its comparison point: answer the
*same* questions from the run's **raw artifact corpus** — the markdown, Word
``.docx``, Jira JSON, and email the run shipped — with classic retrieval-augmented
generation, then score on the same basis.

The pipeline has three stages, only the middle one needs a key:

1. **Index** (keyless). Every ``Artifact`` node's on-disk file is read back to
   text (:func:`extract_text` handles each medium), split into overlapping-free
   word-packed :class:`Chunk`\\ s, and indexed with a dependency-light
   :class:`BM25Index` — pure standard-library BM25, deterministic in the corpus.

2. **Retrieve + answer** (gated). For each question the top-k chunks are retrieved
   and handed to an :class:`~enterprise_sim.core.llm.LLMClient` (the
   ``anthropic_api`` backend in production, gated on ``ANTHROPIC_API_KEY``; any
   stub client in tests) which writes a natural-language answer grounded in that
   context.

3. **Resolve** (keyless). The natural-language answer is mapped back to KG node
   ids by an :class:`AliasResolver` built from the run's ``aliases.jsonl`` and
   ``mentions.jsonl`` (surface form → ``entity_id``), so the RAG answer scores on
   the same node-id basis as the graph runner — longest surface form wins, matches
   are word-bounded, and the predicted id set is sorted for a stable
   :class:`~enterprise_sim.benchmark.score.Prediction`.

Stages 1 and 3 are pure functions of on-disk ground truth and fully unit-testable
without a key; only stage 2 calls the model. :func:`build_runner` wires the three
together for a run directory; :func:`run_rag` is the end-to-end entry point the
``enterprise-sim bench run --runner rag`` CLI drives.
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from email import message_from_bytes
from pathlib import Path
from xml.sax.saxutils import unescape

from enterprise_sim.benchmark.generate import load_world_from_run
from enterprise_sim.benchmark.schema import Benchmark, QAPair
from enterprise_sim.benchmark.score import Prediction, Predictions
from enterprise_sim.core.llm import LLMClient, assemble_prompt
from enterprise_sim.core.world import World

__all__ = [
    "AliasResolver",
    "BM25Index",
    "Chunk",
    "RagRunner",
    "build_runner",
    "extract_text",
    "load_corpus",
    "run_rag",
]

# A token is a maximal run of lowercase letters/digits — the shared vocabulary of
# both the BM25 index and the (lighter) query side, so a query term matches a
# document term iff their surface characters agree.
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase ``text`` into BM25 tokens (maximal alphanumeric runs)."""
    return _WORD_RE.findall(text.lower())


def _read_jsonl(path: str | Path) -> Iterator[dict[str, object]]:
    """Yield each non-blank JSONL row of ``path`` as a dict (empty when absent)."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


# --------------------------------------------------------------------------- #
# Text extraction: read each artifact medium back to plain text.
# --------------------------------------------------------------------------- #

# Pull the visible text out of one Word paragraph: every ``<w:t>`` run's body,
# concatenated. ``<w:t>`` carries the literal characters of a document.
_DOCX_TEXT_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.DOTALL)
# Paragraph and line-break boundaries become newlines so packed chunks keep the
# document's natural structure rather than running every word together.
_DOCX_BREAK_RE = re.compile(r"</w:p>|<w:br\b[^>]*/?>")


def _docx_text(path: Path) -> str:
    """Extract the visible prose (body + threaded comments) from a ``.docx`` package.

    A ``.docx`` is an OOXML zip; the body lives in ``word/document.xml`` and any
    native comment thread in ``word/comments.xml`` (both produced by
    :mod:`enterprise_sim.producers.word_docx`). Paragraph/break tags are turned
    into newlines, every ``<w:t>`` run is concatenated, and XML entities are
    unescaped — no third-party dependency, just :mod:`zipfile`.
    """
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for member in ("word/document.xml", "word/comments.xml"):
            if member not in names:
                continue
            xml = zf.read(member).decode("utf-8")
            xml = _DOCX_BREAK_RE.sub("\n", xml)
            parts.extend(unescape(match) for match in _DOCX_TEXT_RE.findall(xml))
    return "\n".join(part for part in (p.strip() for p in parts) if part)


def _json_text(path: Path) -> str:
    """Flatten a JSON artifact (e.g. a Jira issue) to its string leaves, in order.

    Walks the document depth-first and emits every string value — summaries,
    descriptions, labels, comment bodies, people — one per line. Keys and
    non-string scalars are dropped; the result is the retrievable prose of the
    issue without any schema assumptions.
    """
    lines: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text:
                lines.append(text)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(json.loads(path.read_text(encoding="utf-8")))
    return "\n".join(lines)


def _eml_text(path: Path) -> str:
    """Extract the subject and plain-text body from an ``.eml`` email message."""
    message = message_from_bytes(path.read_bytes())
    parts: list[str] = []
    subject = message.get("subject")
    if subject:
        parts.append(str(subject))
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace").strip())
    return "\n".join(part for part in parts if part)


def extract_text(path: str | Path) -> str:
    """Read an artifact file back to plain text, dispatching on its extension.

    Markdown/plain text is returned verbatim; ``.docx`` is unzipped to its OOXML
    prose, ``.json`` is flattened to its string leaves (Jira issues), and ``.eml``
    yields its subject plus text body. Any other suffix falls back to a UTF-8 read.
    """
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix == ".docx":
        return _docx_text(file)
    if suffix == ".json":
        return _json_text(file)
    if suffix == ".eml":
        return _eml_text(file)
    return file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Chunking + corpus loading.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage of an artifact, tagged with the artifact's node id.

    Attributes:
        artifact_id: The KG ``Artifact`` node id this passage came from — what a
            provenance answer must resolve to.
        path: The artifact's run-relative path (for debugging / display).
        index: The chunk's 0-based position within its artifact.
        text: The passage text fed to retrieval and shown to the model.
    """

    artifact_id: str
    path: str
    index: int
    text: str


def _chunk_text(text: str, target_words: int) -> Iterator[str]:
    """Pack ``text`` into chunks of about ``target_words`` words on paragraph lines.

    Blank-line-separated paragraphs are greedily accumulated until adding the next
    would exceed ``target_words``, then flushed — so a chunk never splits a
    paragraph and short paragraphs share a chunk. A lone paragraph longer than the
    target is emitted whole. The split is deterministic in the text.
    """
    paragraphs = [para.strip() for para in re.split(r"\n\s*\n", text) if para.strip()]
    current: list[str] = []
    count = 0
    for para in paragraphs:
        words = len(para.split())
        if current and count + words > target_words:
            yield "\n\n".join(current)
            current, count = [], 0
        current.append(para)
        count += words
    if current:
        yield "\n\n".join(current)


def load_corpus(run_dir: str | Path, world: World, *, target_words: int = 150) -> list[Chunk]:
    """Read every artifact in ``world`` back to text and split it into :class:`Chunk`\\ s.

    Iterates the gold ``Artifact`` nodes in id order (so the corpus is
    deterministic), resolves each node's ``path`` prop to its on-disk file under
    ``run_dir``, extracts the file's text by medium (:func:`extract_text`), and
    packs it into ~``target_words``-word chunks. Each chunk carries its source
    artifact's node id, so a retrieved passage maps straight back to a KG id.
    Artifacts with no ``path`` prop or no file on disk are skipped.
    """
    base = Path(run_dir)
    chunks: list[Chunk] = []
    for node in world.nodes_by_type("Artifact"):
        rel_path = node.props.get("path")
        if not isinstance(rel_path, str):
            continue
        file = base / rel_path
        if not file.is_file():
            continue
        text = extract_text(file)
        for index, body in enumerate(_chunk_text(text, target_words)):
            chunks.append(Chunk(artifact_id=node.id, path=rel_path, index=index, text=body))
    return chunks


# --------------------------------------------------------------------------- #
# BM25 retrieval (standard-library, deterministic).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BM25Index:
    """A dependency-light BM25 index over a list of :class:`Chunk`\\ s.

    BM25 ranks a chunk for a query by summing, over the query terms it contains, an
    IDF weight times a length-normalized term-frequency saturation
    (:data:`k1`/:data:`b` are the standard knobs). The index is a pure function of
    the corpus — :meth:`build` precomputes per-chunk term frequencies, document
    frequencies, IDF, and the average length — so :meth:`search` is deterministic:
    equal scores break ties by chunk order, and only positively-scoring chunks are
    returned.
    """

    chunks: tuple[Chunk, ...]
    term_freqs: tuple[Mapping[str, int], ...]
    lengths: tuple[int, ...]
    idf: Mapping[str, float]
    avg_length: float
    k1: float = 1.5
    b: float = 0.75

    @classmethod
    def build(cls, chunks: Iterable[Chunk], *, k1: float = 1.5, b: float = 0.75) -> BM25Index:
        """Build a :class:`BM25Index` over ``chunks`` (computes IDF + length norms)."""
        ordered = tuple(chunks)
        token_lists = [_tokenize(chunk.text) for chunk in ordered]
        term_freqs = tuple(Counter(tokens) for tokens in token_lists)
        lengths = tuple(len(tokens) for tokens in token_lists)

        doc_freq: Counter[str] = Counter()
        for freqs in term_freqs:
            doc_freq.update(freqs.keys())

        n_docs = len(ordered)
        # Robertson/Spärck-Jones IDF with the +1 shift, so a term in every document
        # still contributes a small positive weight rather than going negative.
        idf = {
            term: math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()
        }
        avg_length = sum(lengths) / n_docs if n_docs else 0.0
        return cls(
            chunks=ordered,
            term_freqs=term_freqs,
            lengths=lengths,
            idf=idf,
            avg_length=avg_length,
            k1=k1,
            b=b,
        )

    def _score(self, query_terms: Sequence[str], doc: int) -> float:
        """The BM25 score of chunk ``doc`` for ``query_terms``."""
        if self.avg_length == 0.0:
            return 0.0
        freqs = self.term_freqs[doc]
        length = self.lengths[doc]
        norm = self.k1 * (1.0 - self.b + self.b * length / self.avg_length)
        score = 0.0
        for term in query_terms:
            freq = freqs.get(term, 0)
            if freq == 0:
                continue
            score += self.idf.get(term, 0.0) * (freq * (self.k1 + 1.0)) / (freq + norm)
        return score

    def search(self, query: str, k: int = 5) -> list[tuple[Chunk, float]]:
        """Return the ``k`` highest-scoring chunks for ``query`` (positive scores only).

        Ranks every chunk by :meth:`_score`, keeps those with a positive score
        (a zero means the chunk shares no query term), and returns the top ``k`` as
        ``(chunk, score)`` pairs. Ties break by chunk order, so the result is
        deterministic; fewer than ``k`` pairs come back when fewer chunks match.
        """
        query_terms = _tokenize(query)
        scored = ((index, self._score(query_terms, index)) for index in range(len(self.chunks)))
        ranked = sorted(
            (pair for pair in scored if pair[1] > 0.0),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return [(self.chunks[index], score) for index, score in ranked[:k]]


# --------------------------------------------------------------------------- #
# Surface-form → KG node id resolution.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AliasResolver:
    """Map natural-language surface forms back to KG node ids (``aliases`` + ``mentions``).

    Built from a run's ``kg/aliases.jsonl`` (each entity's canonical name and
    aliases) and ``kg/mentions.jsonl`` (every grounded surface form → ``entity_id``),
    this resolves a free-text answer to the set of node ids it names. Matching is
    case-insensitive and word-bounded, and longer surface forms win over the
    shorter forms they contain (``"Cleo Costa"`` is matched before ``"Cleo"``), so
    a name is attributed to the most specific entity the text supports. One surface
    form may be shared by several entities (e.g. an ambiguous first name); all are
    returned.
    """

    by_surface: Mapping[str, frozenset[str]]
    _matcher: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def of(cls, mapping: Mapping[str, Iterable[str]]) -> AliasResolver:
        """Build from a ``surface_form -> entity_ids`` mapping (surfaces lowercased)."""
        by_surface = {
            surface.strip().lower(): frozenset(ids)
            for surface, ids in mapping.items()
            if surface.strip()
        }
        return cls(by_surface=by_surface, _matcher=_build_matcher(by_surface))

    @classmethod
    def from_run(cls, run_dir: str | Path) -> AliasResolver:
        """Build from a run's ``aliases.jsonl`` and ``mentions.jsonl`` answer key."""
        kg = Path(run_dir) / "kg"
        mapping: dict[str, set[str]] = defaultdict(set)

        for record in _read_jsonl(kg / "aliases.jsonl"):
            entity_id = record.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            aliases = record.get("aliases")
            surfaces = [record.get("canonical")]
            if isinstance(aliases, list):
                surfaces.extend(aliases)
            for surface in surfaces:
                if isinstance(surface, str) and surface.strip():
                    mapping[surface.strip().lower()].add(entity_id)

        for record in _read_jsonl(kg / "mentions.jsonl"):
            surface = record.get("surface_form")
            entity_id = record.get("entity_id")
            if isinstance(surface, str) and surface.strip() and isinstance(entity_id, str):
                mapping[surface.strip().lower()].add(entity_id)

        return cls.of(mapping)

    def resolve(self, text: str) -> frozenset[str]:
        """Return the set of entity ids whose surface forms appear in ``text``."""
        if self._matcher is None:
            return frozenset()
        ids: set[str] = set()
        for match in self._matcher.finditer(text.lower()):
            ids.update(self.by_surface.get(match.group(0), ()))
        return frozenset(ids)


def _build_matcher(by_surface: Mapping[str, frozenset[str]]) -> re.Pattern[str] | None:
    """Compile a word-bounded, longest-first alternation over the known surfaces.

    Surfaces are escaped and ordered longest-first so the alternation prefers the
    most specific form, and ``re.finditer`` consumes each match's span, so a longer
    surface (``"cleo costa"``) is never also matched as the shorter form it
    contains. Returns ``None`` when there are no surfaces to match.
    """
    if not by_surface:
        return None
    surfaces = sorted(by_surface, key=len, reverse=True)
    alternation = "|".join(re.escape(surface) for surface in surfaces)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


# --------------------------------------------------------------------------- #
# The runner: retrieve, answer, resolve.
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You answer questions about a company using ONLY the provided document "
    "excerpts. Name the specific people, teams, departments, projects, goals, or "
    "documents that answer the question, by their exact names as written in the "
    "excerpts. If the excerpts do not contain the answer, say so plainly. Do not "
    "invent names that are not in the excerpts."
)


def _format_context(hits: Sequence[tuple[Chunk, float]]) -> str:
    """Render retrieved chunks as a numbered, source-labelled context block."""
    if not hits:
        return "(no relevant documents found)"
    blocks = [
        f"[{i}] (source: {chunk.path})\n{chunk.text}" for i, (chunk, _score) in enumerate(hits, 1)
    ]
    return "\n\n".join(blocks)


def _brief(question: str, context: str) -> str:
    """The volatile per-question user brief: the excerpts then the question."""
    return f"Document excerpts:\n\n{context}\n\nQuestion: {question}\n\nAnswer:"


@dataclass(frozen=True, slots=True)
class RagRunner:
    """Answer the benchmark from the corpus: retrieve, ask the model, resolve to ids.

    Holds the keyless halves of the pipeline — a built :class:`BM25Index` and an
    :class:`AliasResolver` — and applies the model in between. :meth:`answer`
    grades one question; :meth:`run` produces a full
    :class:`~enterprise_sim.benchmark.score.Predictions` set. Both take the
    :class:`~enterprise_sim.core.llm.LLMClient` explicitly, so a deterministic stub
    drives the keyless tests and the ``anthropic_api`` client drives production.
    """

    index: BM25Index
    resolver: AliasResolver
    top_k: int = 5

    def answer(self, pair: QAPair, client: LLMClient, *, model: str | None = None) -> Prediction:
        """Retrieve, ask ``client`` to answer ``pair``, and resolve the answer to ids."""
        hits = self.index.search(pair.question, self.top_k)
        prompt = assemble_prompt(system=_SYSTEM, brief=_brief(pair.question, _format_context(hits)))
        result = client.generate_content(prompt, model=model)
        predicted = self.resolver.resolve(result.content)
        return Prediction(qa_id=pair.id, predicted_ids=tuple(sorted(predicted)))

    def run(
        self, benchmark: Iterable[QAPair], client: LLMClient, *, model: str | None = None
    ) -> Predictions:
        """Answer every pair in ``benchmark`` and collect a :class:`Predictions` set."""
        return Predictions.of(self.answer(pair, client, model=model) for pair in benchmark)


def build_runner(run_dir: str | Path, world: World | None = None, *, top_k: int = 5) -> RagRunner:
    """Wire a :class:`RagRunner` for ``run_dir``: index the corpus + load the resolver.

    Reconstructs the gold :class:`~enterprise_sim.core.world.World` from the run
    (unless one is supplied), reads and indexes the artifact corpus, and builds the
    surface-form resolver from the run's answer key — the whole keyless half of the
    pipeline, ready to answer questions once given a client.
    """
    world = world if world is not None else load_world_from_run(run_dir)
    index = BM25Index.build(load_corpus(run_dir, world))
    resolver = AliasResolver.from_run(run_dir)
    return RagRunner(index=index, resolver=resolver, top_k=top_k)


def run_rag(
    run_dir: str | Path,
    benchmark: Benchmark,
    client: LLMClient,
    *,
    top_k: int = 5,
    model: str | None = None,
    world: World | None = None,
) -> Predictions:
    """End-to-end RAG baseline: build the runner for ``run_dir`` and answer ``benchmark``.

    The convenience entry point the CLI drives — equivalent to
    :func:`build_runner` followed by :meth:`RagRunner.run`. Requires a working
    ``client`` for the retrieval-augmented answer step (gated on a key for the
    ``anthropic_api`` backend); indexing and id-resolution need none.
    """
    runner = build_runner(run_dir, world=world, top_k=top_k)
    return runner.run(benchmark, client, model=model)
