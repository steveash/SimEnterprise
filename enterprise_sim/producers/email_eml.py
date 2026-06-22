"""RFC 5322 / MIME ``.eml`` email-thread renderer (ARCHITECTURE.md §4 Registry-4, §9).

`python-docx` proved threading for Word; e-mail threading is native to the format:
a conversation is a chain of RFC 5322 messages linked by three headers —
``Message-ID`` (each message's stable id), ``In-Reply-To`` (its immediate parent),
and ``References`` (the full ancestor chain). Outlook and every other MUA render a
thread from exactly those headers, so a *thread* is faithfully captured by the
**most recent** message carrying the complete ``References`` chain plus the quoted
history of its ancestors in the body — which is what :func:`render_thread` emits as
one self-contained ``.eml``.

The module is **standard-library only** (``email`` + ``email.policy.SMTP``), so it
carries no new dependency and the output is RFC-valid by construction: round-trip it
through :func:`email.message_from_bytes` and every header parses. Output is
deterministic for identical input — ids and dates are supplied by the caller, never
generated — so the same thread renders byte-identically (D10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage as _MimeMessage
from email.policy import SMTP
from email.utils import format_datetime

__all__ = [
    "EmailMessage",
    "EmailThread",
    "Participant",
    "render_message",
    "render_thread",
]


@dataclass(frozen=True, slots=True)
class Participant:
    """A named mailbox: a display name plus its e-mail address.

    ``address`` is a full ``local@domain`` mailbox; ``name`` is the human display
    name Outlook shows. Rendered as ``Name <local@domain>`` in address headers.
    """

    name: str
    address: str

    def as_address(self) -> Address:
        """Return the :class:`email.headerregistry.Address` for this mailbox."""
        local, _, domain = self.address.partition("@")
        return Address(display_name=self.name, username=local, domain=domain)


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One message in a thread (a single RFC 5322 message).

    ``message_id`` is the bare id (no angle brackets) — the renderer wraps it in
    ``<…>``. ``in_reply_to`` is the parent's bare id; ``references`` is the ordered
    ancestor chain of bare ids (oldest first). ``sender`` / ``to`` / ``cc`` are
    resolved mailboxes; ``date`` stamps the ``Date`` header (and the quoted
    attribution line). Frozen so a rendered thread cannot be mutated mid-render.
    """

    message_id: str
    sender: Participant
    to: Sequence[Participant]
    subject: str
    date: datetime
    body: str
    cc: Sequence[Participant] = ()
    in_reply_to: str | None = None
    references: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class EmailThread:
    """An ordered conversation: messages oldest-first.

    The first message starts the thread; each later message replies to the one
    before it. :func:`render_thread` derives the ``In-Reply-To`` / ``References``
    chain from this order, so callers need not wire the headers by hand.
    """

    messages: Sequence[EmailMessage]
    domain: str = "example.com"
    extra_headers: dict[str, str] = field(default_factory=dict)


# --- rendering --------------------------------------------------------------


def render_message(message: EmailMessage, *, extra_headers: dict[str, str] | None = None) -> bytes:
    """Render one :class:`EmailMessage` to RFC 5322 ``.eml`` bytes (CRLF line ends).

    Uses :data:`email.policy.SMTP` so the wire form is standards-compliant —
    folded long headers, CRLF terminators, a ``MIME-Version`` and a text body
    part — and round-trips through :func:`email.message_from_bytes`.
    """
    mime = _MimeMessage(policy=SMTP)
    mime["From"] = message.sender.as_address()
    mime["To"] = [p.as_address() for p in message.to]
    if message.cc:
        mime["Cc"] = [p.as_address() for p in message.cc]
    mime["Subject"] = message.subject
    mime["Date"] = format_datetime(message.date)
    mime["Message-ID"] = _angle(message.message_id)
    if message.in_reply_to:
        mime["In-Reply-To"] = _angle(message.in_reply_to)
    if message.references:
        mime["References"] = " ".join(_angle(ref) for ref in message.references)
    for name, value in (extra_headers or {}).items():
        mime[name] = value
    mime.set_content(message.body)
    return mime.as_bytes()


def render_thread(thread: EmailThread) -> bytes:
    """Render ``thread`` to a single ``.eml``: the latest message + quoted history.

    The newest message carries the full ``References`` chain and ``In-Reply-To`` of
    its parent; its body is the latest reply followed by the standard nested
    ``On <date>, <name> wrote:`` quoting of every ancestor. The result is one valid
    RFC 5322 message that a MUA threads and displays as the whole conversation.
    """
    if not thread.messages:
        raise ValueError("a thread needs at least one message")
    messages = list(thread.messages)
    latest = messages[-1]
    body = latest.body
    if len(messages) > 1:
        body = f"{latest.body}\n\n{_quoted_history(messages, len(messages) - 2)}"
    threaded = EmailMessage(
        message_id=latest.message_id,
        sender=latest.sender,
        to=latest.to,
        cc=latest.cc,
        subject=latest.subject,
        date=latest.date,
        body=body,
        in_reply_to=messages[-2].message_id if len(messages) > 1 else latest.in_reply_to,
        references=[m.message_id for m in messages[:-1]] or list(latest.references),
    )
    return render_message(threaded, extra_headers=thread.extra_headers or None)


# --- helpers ----------------------------------------------------------------


def _angle(message_id: str) -> str:
    """Wrap a bare ``Message-ID`` in the angle brackets RFC 5322 requires."""
    bare = message_id.strip()
    if bare.startswith("<") and bare.endswith(">"):
        return bare
    return f"<{bare}>"


def _attribution(message: EmailMessage) -> str:
    """The ``On <date>, <sender> wrote:`` line that introduces a quoted message."""
    return f"On {format_datetime(message.date)}, {message.sender.name} wrote:"


def _quoted_history(messages: Sequence[EmailMessage], index: int) -> str:
    """Quote ``messages[index]`` and all its ancestors as nested ``>`` blocks.

    Each recursion adds one quote level, so the immediate parent is ``> ``-quoted,
    its parent ``> > ``-quoted, and so on — the conventional reply-chain rendering.
    """
    message = messages[index]
    lines = [_attribution(message), *(message.body.splitlines() or [""])]
    if index > 0:
        lines.append("")
        lines.extend(_quoted_history(messages, index - 1).splitlines())
    return "\n".join(f"> {line}" if line else ">" for line in lines)
