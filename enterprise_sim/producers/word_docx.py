"""Production OOXML ``.docx`` builder: prose body + native threaded comments (§9, D8).

The :mod:`~enterprise_sim.producers.word_ooxml_spike` spike proved the technique
end to end on a fixed sample. This module is the generalized builder the ``word``
producer drives: an arbitrary multi-paragraph body, **zero or more** native
threaded comments anchored to a span of the body, attributed to real people with
in-window timestamps. It reuses the spike's validated low-level OOXML primitives
(namespace/relationship/content-type constants and the id/date/run/anchor
helpers) so there is one source of OOXML truth; only the *assembly* — which parts
exist, and the conditional comment wiring — lives here.

Two cases:

* **No comments** — a minimal, valid package (``[Content_Types].xml`` + the two
  relationship parts + ``word/document.xml``). No comment parts are declared, so
  Word does not expect parts that are absent.
* **With comments** — the four comment parts
  (``comments``/``commentsExtended``/``commentsIds``/``people``) are added and the
  anchor span is wrapped with ``commentRangeStart``/``End`` + ``commentReference``
  runs, exactly as the spike does, so Word renders a native reply thread.

Output is deterministic for identical input (fixed zip epoch, D10): the same
document always produces byte-identical ``.docx`` bytes.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import quoteattr

from enterprise_sim.producers.word_ooxml_spike import (
    _CT,
    _CT_COMMENTS,
    _CT_COMMENTS_EXT,
    _CT_COMMENTS_IDS,
    _CT_MAIN,
    _CT_PEOPLE,
    _OFFICE_DOC,
    _PR,
    _REL_COMMENTS,
    _REL_COMMENTS_EXTENDED,
    _REL_COMMENTS_IDS,
    _REL_PEOPLE,
    _W,
    _W14,
    _W15,
    _W16CID,
    _ZIP_EPOCH,
    _durable_id,
    _fmt_date,
    _para_id,
    _run,
    _split_anchor,
)

__all__ = ["DocxComment", "DocxDocument", "build_docx"]


@dataclass(frozen=True)
class DocxComment:
    """One native comment in the document's thread.

    ``parent`` is the index (within :attr:`DocxDocument.comments`) of the comment
    this one replies to, or ``None`` for a top-level comment — the engine maps a
    KG ``Person`` and an in-window timestamp onto these fields. Several top-level
    comments (each with ``parent=None``) plus their replies form independent
    threads anchored to the same span.
    """

    author: str
    initials: str
    text: str
    timestamp: datetime
    parent: int | None = None


@dataclass(frozen=True)
class DocxDocument:
    """A renderable document: body paragraphs and an optional anchored comment thread.

    ``anchor`` must be a substring of ``body[anchor_paragraph]`` whenever
    ``comments`` is non-empty; the whole thread attaches to that span. With no
    comments the anchor is unused. ``comments`` is ordered; replies reference
    earlier comments by index.
    """

    body: Sequence[str]
    comments: Sequence[DocxComment] = field(default_factory=tuple)
    anchor: str | None = None
    anchor_paragraph: int = 0


def build_docx(doc: DocxDocument) -> bytes:
    """Build a complete, valid ``.docx`` (as bytes) for ``doc``.

    Emits the comment parts only when ``doc.comments`` is non-empty. Raises
    ``ValueError`` for an empty body, or for a commented document whose ``anchor``
    is missing or not found in its anchor paragraph (a span Word could not attach
    the thread to). Output is deterministic for identical input.
    """
    if not doc.body:
        raise ValueError("a docx needs at least one body paragraph")
    has_comments = bool(doc.comments)
    if has_comments:
        if not doc.anchor:
            raise ValueError("a commented docx needs an anchor span")
        if not (0 <= doc.anchor_paragraph < len(doc.body)):
            raise ValueError(f"anchor_paragraph {doc.anchor_paragraph} out of range")
        # Validate the anchor is present now (rather than failing deep in rendering).
        _split_anchor(doc.body[doc.anchor_paragraph], doc.anchor)

    parts: dict[str, str] = {
        "[Content_Types].xml": _render_content_types(has_comments),
        "_rels/.rels": _render_root_rels(),
        "word/_rels/document.xml.rels": _render_document_rels(has_comments),
        "word/document.xml": _render_document(doc),
    }
    if has_comments:
        parts["word/comments.xml"] = _render_comments(doc.comments)
        parts["word/commentsExtended.xml"] = _render_comments_extended(doc.comments)
        parts["word/commentsIds.xml"] = _render_comments_ids(doc.comments)
        parts["word/people.xml"] = _render_people(doc.comments)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content.encode("utf-8"))
    return buf.getvalue()


# --- part renderers --------------------------------------------------------


def _render_content_types(has_comments: bool) -> str:
    overrides = [f'<Override PartName="/word/document.xml" ContentType="{_CT_MAIN}"/>']
    if has_comments:
        overrides += [
            f'<Override PartName="/word/comments.xml" ContentType="{_CT_COMMENTS}"/>',
            f'<Override PartName="/word/commentsExtended.xml" ContentType="{_CT_COMMENTS_EXT}"/>',
            f'<Override PartName="/word/commentsIds.xml" ContentType="{_CT_COMMENTS_IDS}"/>',
            f'<Override PartName="/word/people.xml" ContentType="{_CT_PEOPLE}"/>',
        ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CT}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{''.join(overrides)}"
        "</Types>"
    )


def _render_root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PR}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_DOC}/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )


def _render_document_rels(has_comments: bool) -> str:
    rels: list[str] = []
    if has_comments:
        rels += [
            f'<Relationship Id="rId1" Type="{_REL_COMMENTS}" Target="comments.xml"/>',
            f'<Relationship Id="rId2" Type="{_REL_COMMENTS_EXTENDED}" '
            'Target="commentsExtended.xml"/>',
            f'<Relationship Id="rId3" Type="{_REL_COMMENTS_IDS}" Target="commentsIds.xml"/>',
            f'<Relationship Id="rId4" Type="{_REL_PEOPLE}" Target="people.xml"/>',
        ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PR}">{"".join(rels)}</Relationships>'
    )


def _render_document(doc: DocxDocument) -> str:
    n_comments = len(doc.comments)
    paras: list[str] = []
    for i, text in enumerate(doc.body):
        if n_comments and i == doc.anchor_paragraph:
            assert doc.anchor is not None
            before, anchored, after = _split_anchor(text, doc.anchor)
            inner: list[str] = []
            if before:
                inner.append(_run(before))
            # Every comment (root and reply) gets a range + reference around the same
            # span; the parent/child threading lives in commentsExtended.xml.
            for cid in range(n_comments):
                inner.append(f'<w:commentRangeStart w:id="{cid}"/>')
            inner.append(_run(anchored))
            for cid in range(n_comments):
                inner.append(f'<w:commentRangeEnd w:id="{cid}"/>')
                inner.append(f'<w:r><w:commentReference w:id="{cid}"/></w:r>')
            if after:
                inner.append(_run(after))
            paras.append(f"<w:p>{''.join(inner)}</w:p>")
        else:
            paras.append(f"<w:p>{_run(text)}</w:p>")
    body = "".join(paras)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">'
        f"<w:body>{body}"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )


def _render_comments(comments: Sequence[DocxComment]) -> str:
    items: list[str] = []
    for i, c in enumerate(comments):
        items.append(
            f'<w:comment w:id="{i}" w:author={quoteattr(c.author)} '
            f"w:date={quoteattr(_fmt_date(c.timestamp))} w:initials={quoteattr(c.initials)}>"
            f'<w:p w14:paraId="{_para_id(i)}" w14:textId="{_para_id(i)}">'
            f"{_run(c.text)}</w:p>"
            "</w:comment>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:comments xmlns:w="{_W}" xmlns:w14="{_W14}">{"".join(items)}</w:comments>'
    )


def _render_comments_extended(comments: Sequence[DocxComment]) -> str:
    items: list[str] = []
    for i, c in enumerate(comments):
        parent_attr = ""
        if c.parent is not None:
            parent_attr = f' w15:paraIdParent="{_para_id(c.parent)}"'
        items.append(f'<w15:commentEx w15:paraId="{_para_id(i)}"{parent_attr} w15:done="0"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w15:commentsEx xmlns:w15="{_W15}">{"".join(items)}</w15:commentsEx>'
    )


def _render_comments_ids(comments: Sequence[DocxComment]) -> str:
    items = [
        f'<w16cid:commentId w16cid:paraId="{_para_id(i)}" w16cid:durableId="{_durable_id(i)}"/>'
        for i in range(len(comments))
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w16cid:commentsIds xmlns:w16cid="{_W16CID}">{"".join(items)}</w16cid:commentsIds>'
    )


def _render_people(comments: Sequence[DocxComment]) -> str:
    # One <w15:person> per distinct author, in first-seen order.
    seen: dict[str, None] = {}
    for c in comments:
        seen.setdefault(c.author, None)
    items = [
        f"<w15:person w15:author={quoteattr(author)}>"
        f'<w15:presenceInfo w15:providerId="None" w15:userId={quoteattr(author)}/>'
        "</w15:person>"
        for author in seen
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w15:people xmlns:w15="{_W15}">{"".join(items)}</w15:people>'
    )
