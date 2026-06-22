"""Tests for the word producer + its OOXML builder (ARCHITECTURE.md §4, §9, §16).

Acceptance (esim-6ce1cd10): document deliverables render as ``.docx`` with native
threaded comments attributed to real people, with in-window timestamps; the files
open/validate. We confirm validity *structurally* (a valid OOXML zip whose parts
are well-formed and wired by ``w14:paraId`` / ``w15:paraIdParent``) — the
"opens in Word" check is a human one, as for the spike — plus the KG facts, the
docx-medium mentions, determinism, and the binding-map rebind.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest
from enterprise_sim.assembly.corpus import _producer_for
from enterprise_sim.core.events import Deliverable, Event
from enterprise_sim.core.llm import LLMClient, LLMConfig
from enterprise_sim.core.world import Node, World
from enterprise_sim.producers import WordProducer
from enterprise_sim.producers.word_docx import DocxComment, DocxDocument, build_docx

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_NS = {"w": _W, "w15": _W15}

_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_T_EVENT = datetime(2026, 6, 12, 14, 0, tzinfo=UTC)


def _person(node_id: str, name: str, *, aliases: list[str] | None = None) -> Node:
    return Node(node_id, "Person", _T0, props={"name": name}, aliases=aliases or [])


def _world() -> World:
    world = World()
    world.add_node(_person("person:ada", "Ada Lovelace", aliases=["Ada"]))
    world.add_node(_person("person:alan", "Alan Turing"))
    world.add_node(_person("person:grace", "Grace Hopper"))
    world.add_node(Node("project:payments", "Project", _T0, props={"name": "Payments Platform"}))
    world.add_node(Node("artifact:design-pay", "Artifact", _T0, props={"title": "Payments Design"}))
    return world


def _event(*, reviewers: list[str] | None = None) -> Event:
    return Event(
        id="evt:status-w12",
        type="DeliverableDrafted",
        timestamp=_T_EVENT,
        actors={
            "author": ["person:ada"],
            "reviewers": ["person:alan", "person:grace"] if reviewers is None else reviewers,
        },
        initiative="init:payments",
        project="project:payments",
        subjects=["project:payments"],
        deliverable=Deliverable(kind="design_doc", medium="document"),
        payload={"topic": "payments rollout", "title": "Payments Design Doc"},
    )


def _client() -> LLMClient:
    return LLMClient.from_config(LLMConfig(backend="fake", cache_enabled=False))


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _part(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


# -- end-to-end producer ----------------------------------------------------


def test_produce_emits_valid_docx_bytes() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    assert produced.fmt == "docx"
    assert produced.path.endswith(".docx")
    assert produced.is_binary and produced.binary_body is not None
    # A valid OOXML package: opens as a zip with the main document part.
    zf = _open(produced.binary_body)
    names = set(zf.namelist())
    assert {"[Content_Types].xml", "word/document.xml"}.issubset(names)
    # Every part parses as well-formed XML.
    for name in names:
        if name.endswith(".xml") or name.endswith(".rels"):
            ET.fromstring(zf.read(name))


def test_comments_attributed_to_real_people_with_in_window_timestamps() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    assert produced.binary_body is not None
    zf = _open(produced.binary_body)
    comments = _part(zf, "word/comments.xml").findall("w:comment", _NS)
    assert len(comments) == 2
    authors = [c.attrib[f"{{{_W}}}author"] for c in comments]
    # Attributed to the real reviewer display names (resolved from the KG).
    assert authors == ["Alan Turing", "Grace Hopper"]
    dates = [c.attrib[f"{{{_W}}}date"] for c in comments]
    # In-window: timestamps are derived from the in-window draft instant (14:00),
    # opening the review thread an hour later and spacing 45 min apart.
    assert dates == ["2026-06-12T15:00:00Z", "2026-06-12T15:45:00Z"]


def test_comments_are_natively_threaded() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    assert produced.binary_body is not None
    ext = _part(_open(produced.binary_body), "word/commentsExtended.xml")
    els = ext.findall("w15:commentEx", _NS)
    by_para = {el.attrib[f"{{{_W15}}}paraId"]: el for el in els}
    # The first comment is top-level; the second replies to it (native reply chain).
    assert f"{{{_W15}}}paraIdParent" not in by_para["00000001"].attrib
    assert by_para["00000002"].attrib[f"{{{_W15}}}paraIdParent"] == "00000001"


def test_people_part_lists_distinct_reviewers() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    assert produced.binary_body is not None
    people = _part(_open(produced.binary_body), "word/people.xml")
    authors = {el.attrib[f"{{{_W15}}}author"] for el in people.findall("w15:person", _NS)}
    assert authors == {"Alan Turing", "Grace Hopper"}


def test_produce_without_reviewers_is_a_valid_plain_docx() -> None:
    # No reviewers → no comment thread → a valid comment-less docx (no comment parts).
    produced = WordProducer().produce(_event(reviewers=[]), _world(), _client())
    assert produced.binary_body is not None
    names = set(_open(produced.binary_body).namelist())
    assert "word/document.xml" in names
    assert "word/comments.xml" not in names


def test_produce_tags_docx_medium_mentions() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    entities = {m.entity_id for m in produced.mentions}
    # Author (header) and the reviewer-commenters all surface in the projection text.
    assert {"person:ada", "person:alan", "person:grace"}.issubset(entities)
    for mention in produced.mentions:
        assert mention.locator.medium == "docx"
        span = produced.body[
            mention.locator.offset : mention.locator.offset + mention.locator.length
        ]
        assert span == mention.surface_form


def test_produce_builds_kg_node_and_edges() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    assert produced.node.type == "Artifact"
    assert produced.node.props["path"] == produced.path
    assert produced.node.props["format"] == "docx"
    by_type = {e.type: e for e in produced.edges}
    assert by_type["authored"].src == "person:ada"
    reviewed = {e.src for e in produced.edges if e.type == "reviewed"}
    assert reviewed == {"person:alan", "person:grace"}
    assert any(e.type == "expresses" and e.dst == "project:payments" for e in produced.edges)


def test_produce_is_deterministic() -> None:
    a = WordProducer().produce(_event(), _world(), _client())
    b = WordProducer().produce(_event(), _world(), _client())
    assert a.binary_body == b.binary_body
    assert a.body == b.body
    assert [m.to_dict() for m in a.mentions] == [m.to_dict() for m in b.mentions]


def test_metadata_records_format_and_comments() -> None:
    produced = WordProducer().produce(_event(), _world(), _client())
    assert produced.metadata["format"] == "docx"
    meta_comments = produced.metadata["comments"]
    assert [c["author"] for c in meta_comments] == ["Alan Turing", "Grace Hopper"]


# -- binding-map rebind -----------------------------------------------------


def test_binding_rebinds_document_kinds_to_word() -> None:
    # The document kinds route to the word producer; everything else falls back to
    # the markdown default.
    assert _producer_for("design_doc").name == "word"
    assert _producer_for("status_report").name == "word"
    assert _producer_for("kickoff_brief").name == "markdown"


# -- the OOXML builder ------------------------------------------------------


def test_build_docx_rejects_empty_body() -> None:
    with pytest.raises(ValueError, match="at least one body paragraph"):
        build_docx(DocxDocument(body=[]))


def test_build_docx_rejects_commented_doc_without_anchor() -> None:
    doc = DocxDocument(body=["Some prose"], comments=[DocxComment("A", "A", "x", _T0)], anchor=None)
    with pytest.raises(ValueError, match="anchor"):
        build_docx(doc)


def test_build_docx_rejects_missing_anchor_substring() -> None:
    doc = DocxDocument(
        body=["Some prose"],
        comments=[DocxComment("A", "A", "x", _T0)],
        anchor="absent phrase",
    )
    with pytest.raises(ValueError, match="anchor"):
        build_docx(doc)


def test_build_docx_is_deterministic() -> None:
    doc = DocxDocument(
        body=["Title", "Body about the rollout."],
        comments=[DocxComment("Ada Lovelace", "AL", "Looks good.", _T0)],
        anchor="rollout",
        anchor_paragraph=1,
    )
    assert build_docx(doc) == build_docx(doc)
