"""Hierarchical chunking tests (esim-nc6.2): markdown headings + Jira fields.

Covers the first reconstruct stage — carving the raw corpus into structure-aligned
:class:`~enterprise_sim.reconstruct.schema.Chunk`\\ s — against both a hand-built
fixture (so the section breadcrumbs, offsets, and field paths are pinned exactly)
and the golden run (so it produces chunks with correct locators, deterministically,
and never reads the gold ``kg/`` subtree).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from enterprise_sim.benchmark.fixtures import golden_run
from enterprise_sim.reconstruct import chunk_jira, chunk_markdown, chunk_run, iter_corpus_files

_MARKDOWN = """\
Intro line before any heading.

# Title

Body of title.

## Section A

Text of A.

### Sub A1

Text of A1.

## Section B

Text of B.
"""


def test_markdown_splits_by_heading_with_breadcrumb_sections() -> None:
    chunks = chunk_markdown(_MARKDOWN, "doc.md")
    got = [(c.section, c.text.splitlines()[0]) for c in chunks]
    assert got == [
        (None, "Intro line before any heading."),
        ("Title", "# Title"),
        ("Title > Section A", "## Section A"),
        ("Title > Section A > Sub A1", "### Sub A1"),
        ("Title > Section B", "## Section B"),
    ]


def test_markdown_offsets_locate_exact_text() -> None:
    for chunk in chunk_markdown(_MARKDOWN, "doc.md"):
        assert _MARKDOWN[chunk.offset : chunk.offset + len(chunk.text)] == chunk.text
        assert chunk.source_path == "doc.md"


def test_markdown_ignores_headings_inside_code_fences() -> None:
    text = "# Real\n\n```\n# not a heading\n```\n\n## Also real\n"
    sections = [c.section for c in chunk_markdown(text, "d.md")]
    assert sections == ["Real", "Real > Also real"]
    # The fenced "# not a heading" stays inside the "Real" chunk, not its own.
    assert "# not a heading" in chunk_markdown(text, "d.md")[0].text


def test_markdown_whole_file_preamble_when_no_headings() -> None:
    chunks = chunk_markdown("Just prose.\n\nMore prose.\n", "flat.md")
    assert len(chunks) == 1
    assert chunks[0].section is None
    assert chunks[0].offset == 0


def test_jira_splits_summary_description_and_comments() -> None:
    raw = json.dumps(
        {
            "key": "BSD-1",
            "fields": {
                "summary": "Fix the widget",
                "description": "  The widget is broken.  ",
                "comment": {
                    "comments": [
                        {"author": {"displayName": "Ada"}, "body": "On it."},
                        {"author": {"displayName": "Cleo"}, "body": "Thanks."},
                    ],
                    "total": 2,
                },
            },
        }
    )
    chunks = chunk_jira(raw, "issue.jira.json")
    assert [(c.section, c.text) for c in chunks] == [
        ("fields.summary", "Fix the widget"),
        ("fields.description", "The widget is broken."),
        ("fields.comment.comments[0].body", "On it."),
        ("fields.comment.comments[1].body", "Thanks."),
    ]
    assert all(c.source_path == "issue.jira.json" and c.offset == 0 for c in chunks)


def test_jira_skips_empty_and_missing_fields() -> None:
    raw = json.dumps({"key": "X", "fields": {"summary": "", "description": "kept"}})
    chunks = chunk_jira(raw, "i.jira.json")
    assert [c.section for c in chunks] == ["fields.description"]


def test_jira_malformed_documents_yield_no_chunks() -> None:
    assert chunk_jira("not json", "a.jira.json") == []
    assert chunk_jira("[1, 2, 3]", "b.jira.json") == []
    assert chunk_jira(json.dumps({"key": "X"}), "c.jira.json") == []


def test_chunk_ids_are_stable_and_locally_unique() -> None:
    text = "# References\n\nsee A\n\n# References\n\nsee B\n"
    chunks = chunk_markdown(text, "d.md")
    # Two identically-titled sections with different bodies get distinct ids...
    assert chunks[0].id != chunks[1].id
    # ...but chunking the same input again reproduces them exactly.
    assert [c.id for c in chunks] == [c.id for c in chunk_markdown(text, "d.md")]


def test_chunk_run_over_golden_produces_located_deterministic_chunks() -> None:
    with tempfile.TemporaryDirectory(prefix="esim-chunk-test-") as tmp:
        run_dir = Path(golden_run(tmp).run_dir)
        chunks = chunk_run(run_dir)

        assert chunks, "golden run should yield chunks from md + jira"
        media = {"jira" if c.source_path.endswith(".jira.json") else "md" for c in chunks}
        assert media == {"md", "jira"}, "both media are chunked"

        # Every chunk carries a resolvable source locator...
        for chunk in chunks:
            assert (run_dir / chunk.source_path).is_file()
            assert chunk.text.strip() == chunk.text and chunk.text
            # ...and the gold KG answer key is never chunked.
            assert not chunk.source_path.startswith("kg/")

        # Deterministic across runs of the same directory.
        assert [c.id for c in chunks] == [c.id for c in chunk_run(run_dir)]


def test_iter_corpus_files_excludes_gold_kg() -> None:
    with tempfile.TemporaryDirectory(prefix="esim-chunk-test-") as tmp:
        run_dir = Path(golden_run(tmp).run_dir)
        files = iter_corpus_files(run_dir)
        rels = [f.relative_to(run_dir).as_posix() for f in files]
        assert rels == sorted(rels), "files are returned in stable sorted order"
        assert all(not r.startswith("kg/") for r in rels)
        assert all(r.endswith(".md") or r.endswith(".jira.json") for r in rels)
        assert any(r.endswith(".jira.json") for r in rels)
