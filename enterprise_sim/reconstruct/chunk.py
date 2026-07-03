"""Hierarchical chunking: raw corpus artifacts → :class:`Chunk`\\ s (esim-nc6.2).

The first stage of the reconstruct pipeline. It reads a run's **raw artifact
corpus** back off disk — *without touching the gold KG* — and carves each
artifact into structure-aligned :class:`Chunk`\\ s, the unit every later stage
(mention detection, extraction) consumes.

This cut handles the two text-native media:

* **Markdown** (``*.md``) — split by heading structure. Each ATX heading opens a
  section that owns the text down to the next heading; the chunk's ``section`` is
  the breadcrumb path of ancestor headings (``"Engineering > Teams > Platform"``),
  and any prose before the first heading becomes a preamble chunk. Fenced code
  blocks are respected so a ``#`` inside a fence is not mistaken for a heading.
* **Jira** (``*.jira.json``) — split by issue field. The ``summary``,
  ``description``, and each ``comment`` body become their own chunk, located by a
  JSON-path ``section`` (``fields.summary``, ``fields.comment.comments[0].body``).

Hierarchical (structure-aware) chunking is deliberate: keeping a section or field
intact — rather than slicing at a fixed word count — preserves the local context a
relation needs, which measurably lifts downstream extraction F1 over fixed-size
windows.

Every chunk keeps a **source locator** for provenance: the run-relative
``source_path`` (matching the gold ``Artifact`` node's ``path`` prop, so a chunk
resolves back to its artifact), a character ``offset`` into the source, and the
structural ``section``. Chunk ids are content-derived (a hash over path + section
+ offset + text), so the whole pass is a pure, deterministic function of the run
directory: identical inputs yield an identical, byte-stable chunk list.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from enterprise_sim.reconstruct.schema import Chunk

__all__ = [
    "chunk_jira",
    "chunk_markdown",
    "chunk_run",
    "iter_corpus_files",
]

# The gold KG lives here; the reconstruct pipeline must read the *raw* corpus, not
# the answer key, so this subtree is skipped during discovery (esim-nc6 epic).
_GOLD_KG_DIR = "kg"

# The two media this cut chunks, by filename suffix. ``.jira.json`` is checked
# before the generic ``.md`` split so the dispatch is unambiguous.
_JIRA_SUFFIX = ".jira.json"
_MARKDOWN_SUFFIX = ".md"

# An ATX markdown heading: 1-6 leading ``#``, a space, the title, optional closing
# ``#`` run. Matched only outside fenced code blocks (see :func:`_iter_headings`).
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
# A code fence opener/closer: three-or-more backticks or tildes at line start.
_CODE_FENCE = re.compile(r"^[ \t]*(?:```+|~~~+)")


def chunk_run(run_dir: str | Path) -> list[Chunk]:
    """Chunk every markdown + Jira artifact under ``run_dir`` into :class:`Chunk`\\ s.

    Walks the run directory for ``*.md`` and ``*.jira.json`` files (skipping the
    gold ``kg/`` subtree), splits each by its document structure, and returns the
    concatenated chunks. Files are visited in sorted run-relative path order and
    chunks are emitted in document order, so the result is deterministic across
    runs. The gold KG is never read — this is a pure function of the raw corpus.
    """
    base = Path(run_dir)
    chunks: list[Chunk] = []
    for path in iter_corpus_files(base):
        source_path = path.relative_to(base).as_posix()
        raw = path.read_text(encoding="utf-8")
        if path.name.endswith(_JIRA_SUFFIX):
            chunks.extend(chunk_jira(raw, source_path))
        else:
            chunks.extend(chunk_markdown(raw, source_path))
    return chunks


def iter_corpus_files(run_dir: str | Path) -> list[Path]:
    """Return the raw markdown + Jira artifact files under ``run_dir``, sorted.

    Recurses the whole run directory but skips the top-level gold ``kg/`` subtree
    (the answer key the reconstruction must not read). The returned paths are
    sorted by their run-relative POSIX form for a stable, cross-platform order.
    """
    base = Path(run_dir)
    gold_kg = base / _GOLD_KG_DIR
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if gold_kg in path.parents:
            continue
        name = path.name
        if name.endswith(_JIRA_SUFFIX) or name.endswith(_MARKDOWN_SUFFIX):
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(base).as_posix())


# --------------------------------------------------------------------------- #
# Markdown: split by heading structure.
# --------------------------------------------------------------------------- #


def chunk_markdown(text: str, source_path: str) -> list[Chunk]:
    """Split markdown ``text`` into one :class:`Chunk` per heading section.

    Each ATX heading opens a section that owns the lines down to the next heading
    of *any* level, so the sections partition the document without overlap. A
    chunk's ``section`` is the breadcrumb of its ancestor headings joined with
    ``" > "``; text before the first heading becomes a preamble chunk with no
    section. Empty (whitespace-only) sections are dropped. ``offset`` points at the
    first non-whitespace character of the chunk within ``text``.
    """
    boundaries = list(_iter_headings(text))
    chunks: list[Chunk] = []

    # Preamble: everything before the first heading (or the whole file if none).
    first_start = boundaries[0][0] if boundaries else len(text)
    preamble = _make_chunk(text, 0, first_start, source_path, None)
    if preamble is not None:
        chunks.append(preamble)

    stack: list[tuple[int, str]] = []
    for i, (start, level, title) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        while stack and stack[-1][0] >= level:
            stack.pop()
        section = " > ".join(t for _, t in stack) + (" > " if stack else "") + title
        stack.append((level, title))
        chunk = _make_chunk(text, start, end, source_path, section)
        if chunk is not None:
            chunks.append(chunk)
    return chunks


def _iter_headings(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(char_offset, level, title)`` for each ATX heading in ``text``.

    Tracks fenced code blocks (```` ``` ````/``~~~``) so a ``#`` inside a fence is
    not treated as a heading. ``char_offset`` is the offset of the heading line's
    first character within ``text``.
    """
    offset = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if _CODE_FENCE.match(stripped):
            in_fence = not in_fence
        elif not in_fence:
            match = _ATX_HEADING.match(stripped)
            if match is not None:
                yield offset, len(match.group(1)), match.group(2).strip()
        offset += len(line)


def _make_chunk(
    text: str,
    start: int,
    end: int,
    source_path: str,
    section: str | None,
) -> Chunk | None:
    """Build a :class:`Chunk` from ``text[start:end]``, or ``None`` if it is blank.

    The stored text is the span stripped of surrounding whitespace; ``offset`` is
    advanced past any stripped leading whitespace so it still locates the text.
    """
    span = text[start:end]
    stripped = span.strip()
    if not stripped:
        return None
    lead = len(span) - len(span.lstrip())
    offset = start + lead
    return Chunk(
        id=_chunk_id(source_path, section, offset, stripped),
        text=stripped,
        source_path=source_path,
        offset=offset,
        section=section,
    )


# --------------------------------------------------------------------------- #
# Jira: split by issue field.
# --------------------------------------------------------------------------- #


def chunk_jira(raw: str, source_path: str) -> list[Chunk]:
    """Split a Jira issue JSON into one :class:`Chunk` per text field.

    Emits, in this order and when present and non-empty: the ``summary``, the
    ``description``, then each ``comment`` body. Each chunk's ``section`` is the
    field's JSON path (``fields.summary``, ``fields.description``,
    ``fields.comment.comments[<i>].body``) — the source locator, since a parsed
    field value has no faithful character offset into the raw JSON, so ``offset``
    is ``0``. A malformed document (not an object, no ``fields``) yields no chunks.
    """
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    fields = data.get("fields")
    if not isinstance(fields, dict):
        return []

    chunks: list[Chunk] = []
    for key in ("summary", "description"):
        chunk = _jira_field_chunk(fields.get(key), source_path, f"fields.{key}")
        if chunk is not None:
            chunks.append(chunk)

    comment = fields.get("comment")
    comments = comment.get("comments") if isinstance(comment, dict) else None
    if isinstance(comments, list):
        for i, item in enumerate(comments):
            body = item.get("body") if isinstance(item, dict) else None
            section = f"fields.comment.comments[{i}].body"
            chunk = _jira_field_chunk(body, source_path, section)
            if chunk is not None:
                chunks.append(chunk)
    return chunks


def _jira_field_chunk(value: Any, source_path: str, section: str) -> Chunk | None:
    """Build a :class:`Chunk` for a Jira field ``value``, or ``None`` if unusable.

    Only non-empty string values become chunks; the ``section`` carries the field
    path and ``offset`` is ``0`` (a parsed field has no faithful source offset).
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return Chunk(
        id=_chunk_id(source_path, section, 0, stripped),
        text=stripped,
        source_path=source_path,
        offset=0,
        section=section,
    )


def _chunk_id(source_path: str, section: str | None, offset: int, text: str) -> str:
    """Return a stable, content-derived chunk id.

    Hashes the source path, section, offset, and text together so the id is stable
    across runs and distinct for otherwise-identical text in different locations
    (e.g. two ``## References`` sections in different files).
    """
    digest = hashlib.sha1(usedforsecurity=False)
    for part in (source_path, section or "", str(offset), text):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]
