"""Structural tests for the native-threaded-comment OOXML spike (ARCHITECTURE.md §9, D8).

These validate the ``.docx`` *structure* that makes Word render a native reply thread — the
parts exist, are well-formed XML, and are wired together by ``w14:paraId`` /
``w15:paraIdParent``. They do not (cannot in CI) launch Word; the spike's "opens cleanly in
Word" acceptance is confirmed by a human opening the doc that
``python -m enterprise_sim.producers.word_ooxml_spike`` writes.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

import pytest
from enterprise_sim.producers.word_ooxml_spike import (
    Comment,
    ThreadedCommentDoc,
    build_threaded_comment_docx,
    sample_doc,
)

# Namespaces used to query the rendered parts.
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_NS = {"w": _W, "w14": _W14, "w15": _W15, "w16cid": _W16CID}

_EXPECTED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/_rels/document.xml.rels",
    "word/document.xml",
    "word/comments.xml",
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/people.xml",
}


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _part(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def test_package_contains_all_ooxml_parts() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    assert _EXPECTED_PARTS.issubset(set(zf.namelist()))


def test_all_parts_are_well_formed_xml() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    for name in _EXPECTED_PARTS:
        # Raises ParseError if any part is malformed.
        ET.fromstring(zf.read(name))


def test_content_types_declares_every_part() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    root = _part(zf, "[Content_Types].xml")
    ct = "{http://schemas.openxmlformats.org/package/2006/content-types}Override"
    declared = {el.attrib["PartName"] for el in root.iter(ct)}
    assert {
        "/word/document.xml",
        "/word/comments.xml",
        "/word/commentsExtended.xml",
        "/word/commentsIds.xml",
        "/word/people.xml",
    }.issubset(declared)


def test_comments_carry_authors_dates_and_para_ids() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    comments = _part(zf, "word/comments.xml")
    els = comments.findall("w:comment", _NS)
    assert len(els) == 3

    first = els[0]
    assert first.attrib[f"{{{_W}}}author"] == "Alice Nguyen"
    assert first.attrib[f"{{{_W}}}initials"] == "AN"
    assert first.attrib[f"{{{_W}}}date"] == "2026-06-01T10:00:00Z"

    # Each comment's paragraph carries a unique, non-zero w14:paraId.
    para_ids = [p.attrib[f"{{{_W14}}}paraId"] for c in els for p in c.findall("w:p", _NS)]
    assert para_ids == ["00000001", "00000002", "00000003"]
    assert "00000000" not in para_ids


def test_commentsextended_threads_replies_to_parents() -> None:
    """The reply thread is established purely by w15:paraIdParent links."""
    zf = _open(build_threaded_comment_docx(sample_doc()))
    ext = _part(zf, "word/commentsExtended.xml")
    els = ext.findall("w15:commentEx", _NS)
    by_para = {el.attrib[f"{{{_W15}}}paraId"]: el for el in els}

    # Root comment has no parent.
    assert f"{{{_W15}}}paraIdParent" not in by_para["00000001"].attrib
    # Reply #2 threads under the root; reply #3 threads under reply #2.
    assert by_para["00000002"].attrib[f"{{{_W15}}}paraIdParent"] == "00000001"
    assert by_para["00000003"].attrib[f"{{{_W15}}}paraIdParent"] == "00000002"


def test_comment_ids_part_assigns_durable_ids() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    ids = _part(zf, "word/commentsIds.xml")
    els = ids.findall("w16cid:commentId", _NS)
    para_ids = {el.attrib[f"{{{_W16CID}}}paraId"] for el in els}
    durable_ids = {el.attrib[f"{{{_W16CID}}}durableId"] for el in els}
    assert para_ids == {"00000001", "00000002", "00000003"}
    # Durable ids must be unique so comments survive round-trips.
    assert len(durable_ids) == 3


def test_people_part_lists_distinct_authors() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    people = _part(zf, "word/people.xml")
    authors = {el.attrib[f"{{{_W15}}}author"] for el in people.findall("w15:person", _NS)}
    # Alice appears twice in the thread but only once as a person.
    assert authors == {"Alice Nguyen", "Bob Carter"}


def test_document_anchors_each_comment_with_range_and_reference() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    doc = _part(zf, "word/document.xml")
    starts = {el.attrib[f"{{{_W}}}id"] for el in doc.iter(f"{{{_W}}}commentRangeStart")}
    ends = {el.attrib[f"{{{_W}}}id"] for el in doc.iter(f"{{{_W}}}commentRangeEnd")}
    refs = {el.attrib[f"{{{_W}}}id"] for el in doc.iter(f"{{{_W}}}commentReference")}
    assert starts == ends == refs == {"0", "1", "2"}


def test_document_preserves_anchor_text() -> None:
    zf = _open(build_threaded_comment_docx(sample_doc()))
    doc = _part(zf, "word/document.xml")
    text = "".join(t.text or "" for t in doc.iter(f"{{{_W}}}t"))
    assert "batch events every five minutes" in text


def test_output_is_deterministic() -> None:
    a = build_threaded_comment_docx(sample_doc())
    b = build_threaded_comment_docx(sample_doc())
    assert a == b


def test_empty_thread_is_rejected() -> None:
    doc = ThreadedCommentDoc(body=["hi"], anchor="hi", thread=[])
    with pytest.raises(ValueError, match="at least one comment"):
        build_threaded_comment_docx(doc)


def test_missing_anchor_is_rejected() -> None:
    doc = ThreadedCommentDoc(
        body=["nothing to see here"],
        anchor="absent phrase",
        thread=[
            Comment(
                author="A",
                initials="A",
                text="x",
                timestamp=datetime(2026, 6, 1, 9, 0, 0),
            )
        ],
    )
    with pytest.raises(ValueError, match="anchor"):
        build_threaded_comment_docx(doc)


def test_xml_special_characters_are_escaped() -> None:
    doc = ThreadedCommentDoc(
        body=["See the <tag> & 'quote' section"],
        anchor="<tag> & 'quote'",
        thread=[
            Comment(
                author='Renée "R&D" O\'Brien',
                initials="RO",
                text="Watch out for < > & in the body.",
                timestamp=datetime(2026, 6, 1, 9, 0, 0),
            )
        ],
    )
    data = build_threaded_comment_docx(doc)
    # Round-trips through the XML parser without error, preserving the values.
    zf = _open(data)
    comments = _part(zf, "word/comments.xml")
    comment = comments.find("w:comment", _NS)
    assert comment is not None
    assert comment.attrib[f"{{{_W}}}author"] == 'Renée "R&D" O\'Brien'
    body_text = "".join(t.text or "" for t in _part(zf, "word/document.xml").iter(f"{{{_W}}}t"))
    assert "<tag> & 'quote'" in body_text
