"""Spike: native Word threaded comments via raw OOXML injection (ARCHITECTURE.md §9, D8).

`python-docx` writes paragraph bodies but has no real support for comments or threaded
replies. Word stores those as separate OOXML parts inside the ``.docx`` zip, linked by
relationships and tied together by ``w14:paraId`` markers:

* ``word/comments.xml``         — comment text + author + date (each ``<w:comment>``'s
                                  paragraph carries a ``w14:paraId``).
* ``word/commentsExtended.xml`` — ``w15:paraIdParent`` links that turn flat comments into
                                  **reply threads** (this is what makes threading native).
* ``word/commentsIds.xml``      — durable ids (``w16cid``) so comments survive round-trips.
* ``word/people.xml``           — author identities (``w15:person``).
* range markers in ``word/document.xml`` — ``commentRangeStart``/``commentRangeEnd`` plus a
                                  ``commentReference`` run anchor each comment to text.

This module proves the technique end-to-end by **building a complete minimal ``.docx`` from
scratch** with the standard library only (``zipfile`` + string XML), so the spike carries no
new dependency and isolates the OOXML risk before the full ``word`` producer is built
(esim-6ce1cd10). The real producer will reuse :func:`build_threaded_comment_docx` (or its
part-rendering helpers) to inject these parts into ``python-docx`` output.

Run ``python -m enterprise_sim.producers.word_ooxml_spike out.docx`` to emit a sample doc and
confirm it opens cleanly in Word with a real reply thread.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

# --- OOXML namespace + relationship constants ---------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_REL_COMMENTS = f"{_OFFICE_DOC}/comments"
_REL_COMMENTS_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_REL_COMMENTS_IDS = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
_REL_PEOPLE = "http://schemas.microsoft.com/office/2011/relationships/people"

_CT_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_CT_COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
_CT_COMMENTS_EXT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
)
_CT_COMMENTS_IDS = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml"
_CT_PEOPLE = "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml"

# Fixed zip timestamp so identical input yields byte-identical output (D10: determinism).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


# --- public data model --------------------------------------------------------------------


@dataclass(frozen=True)
class Comment:
    """One comment in a thread.

    ``parent`` is the index (within the thread sequence) of the comment this one replies to,
    or ``None`` for a top-level comment. The engine will map a KG ``Person`` and an in-window
    timestamp onto these fields.
    """

    author: str
    initials: str
    text: str
    timestamp: datetime
    parent: int | None = None


@dataclass(frozen=True)
class ThreadedCommentDoc:
    """A minimal document: paragraphs of body text, one anchored phrase, and a comment thread.

    ``anchor`` must be a substring of ``body[anchor_paragraph]``; the comment thread attaches
    to that span. ``thread`` is ordered; replies reference earlier comments by index.
    """

    body: Sequence[str]
    anchor: str
    thread: Sequence[Comment]
    anchor_paragraph: int = 0


# --- id derivation ------------------------------------------------------------------------


def _para_id(index: int) -> str:
    """8-hex ``w14:paraId`` for comment ``index``. Never ``00000000`` (reserved by Word)."""
    return format(index + 1, "08X")


def _durable_id(index: int) -> str:
    """Stable 8-hex ``w16cid`` durable id; offset keeps it clear of low reserved values."""
    return format(0x10000000 + index, "08X")


def _fmt_date(ts: datetime) -> str:
    """Format a timestamp as the ``w:date`` UTC form Word expects."""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- part renderers (each returns a complete OOXML part) -----------------------------------


def _render_content_types() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CT}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{_CT_MAIN}"/>'
        f'<Override PartName="/word/comments.xml" ContentType="{_CT_COMMENTS}"/>'
        f'<Override PartName="/word/commentsExtended.xml" ContentType="{_CT_COMMENTS_EXT}"/>'
        f'<Override PartName="/word/commentsIds.xml" ContentType="{_CT_COMMENTS_IDS}"/>'
        f'<Override PartName="/word/people.xml" ContentType="{_CT_PEOPLE}"/>'
        "</Types>"
    )


def _render_root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PR}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_DOC}/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )


def _render_document_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PR}">'
        f'<Relationship Id="rId1" Type="{_REL_COMMENTS}" Target="comments.xml"/>'
        f'<Relationship Id="rId2" Type="{_REL_COMMENTS_EXTENDED}" Target="commentsExtended.xml"/>'
        f'<Relationship Id="rId3" Type="{_REL_COMMENTS_IDS}" Target="commentsIds.xml"/>'
        f'<Relationship Id="rId4" Type="{_REL_PEOPLE}" Target="people.xml"/>'
        "</Relationships>"
    )


def _split_anchor(paragraph: str, anchor: str) -> tuple[str, str, str]:
    """Split ``paragraph`` into (before, anchor, after) around the first occurrence of anchor."""
    start = paragraph.find(anchor)
    if start < 0:
        raise ValueError(f"anchor {anchor!r} not found in paragraph {paragraph!r}")
    end = start + len(anchor)
    return paragraph[:start], paragraph[start:end], paragraph[end:]


def _run(text: str) -> str:
    """A ``<w:r>`` text run that preserves leading/trailing whitespace."""
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _render_document(doc: ThreadedCommentDoc) -> str:
    paras: list[str] = []
    n_comments = len(doc.thread)
    for i, text in enumerate(doc.body):
        if i == doc.anchor_paragraph:
            before, anchored, after = _split_anchor(text, doc.anchor)
            inner: list[str] = []
            if before:
                inner.append(_run(before))
            # Every comment (root and reply) gets a range + reference around the same span;
            # the parent/child threading lives in commentsExtended.xml.
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


def _render_comments(doc: ThreadedCommentDoc) -> str:
    items: list[str] = []
    for i, c in enumerate(doc.thread):
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


def _render_comments_extended(doc: ThreadedCommentDoc) -> str:
    items: list[str] = []
    for i, c in enumerate(doc.thread):
        parent_attr = ""
        if c.parent is not None:
            parent_attr = f' w15:paraIdParent="{_para_id(c.parent)}"'
        items.append(f'<w15:commentEx w15:paraId="{_para_id(i)}"{parent_attr} w15:done="0"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w15:commentsEx xmlns:w15="{_W15}">{"".join(items)}</w15:commentsEx>'
    )


def _render_comments_ids(doc: ThreadedCommentDoc) -> str:
    items = [
        f'<w16cid:commentId w16cid:paraId="{_para_id(i)}" w16cid:durableId="{_durable_id(i)}"/>'
        for i in range(len(doc.thread))
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w16cid:commentsIds xmlns:w16cid="{_W16CID}">{"".join(items)}</w16cid:commentsIds>'
    )


def _render_people(doc: ThreadedCommentDoc) -> str:
    # One <w15:person> per distinct author, in first-seen order.
    seen: dict[str, None] = {}
    for c in doc.thread:
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


# --- assembly -----------------------------------------------------------------------------


def build_threaded_comment_docx(doc: ThreadedCommentDoc) -> bytes:
    """Build a complete ``.docx`` (as bytes) containing ``doc``'s native threaded comments.

    The result is a valid OOXML package: unzip it and you get ``[Content_Types].xml``, the
    package + document relationships, and the five wordprocessing parts wired together so Word
    renders the thread natively. Output is deterministic for identical input.
    """
    if not doc.thread:
        raise ValueError("a threaded-comment doc needs at least one comment")
    if not doc.body:
        raise ValueError("a doc needs at least one body paragraph")

    parts: dict[str, str] = {
        "[Content_Types].xml": _render_content_types(),
        "_rels/.rels": _render_root_rels(),
        "word/_rels/document.xml.rels": _render_document_rels(),
        "word/document.xml": _render_document(doc),
        "word/comments.xml": _render_comments(doc),
        "word/commentsExtended.xml": _render_comments_extended(doc),
        "word/commentsIds.xml": _render_comments_ids(doc),
        "word/people.xml": _render_people(doc),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content.encode("utf-8"))
    return buf.getvalue()


def sample_doc() -> ThreadedCommentDoc:
    """A canonical spike document: a two-author reply thread anchored to a phrase."""
    return ThreadedCommentDoc(
        body=[
            "Q3 Architecture Review",
            "The ingestion pipeline will batch events every five minutes before flushing "
            "to the warehouse.",
            "Owner: Platform team. Target: end of quarter.",
        ],
        anchor="batch events every five minutes",
        anchor_paragraph=1,
        thread=[
            Comment(
                author="Alice Nguyen",
                initials="AN",
                text="Five minutes feels high for the SLA — can we justify it?",
                timestamp=datetime(2026, 6, 1, 10, 0, 0),
                parent=None,
            ),
            Comment(
                author="Bob Carter",
                initials="BC",
                text="Agreed. Let's drop to one minute; the warehouse can absorb it.",
                timestamp=datetime(2026, 6, 1, 11, 30, 0),
                parent=0,
            ),
            Comment(
                author="Alice Nguyen",
                initials="AN",
                text="Works for me. I'll update the design doc.",
                timestamp=datetime(2026, 6, 1, 13, 15, 0),
                parent=1,
            ),
        ],
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Write a sample threaded-comment ``.docx`` so a human can confirm it opens in Word."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    out = args[0] if args else "threaded_comment_spike.docx"
    data = build_threaded_comment_docx(sample_doc())
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"wrote {len(data)} bytes to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
