"""RFC 5545 iCalendar ``.ics`` renderer (ARCHITECTURE.md §4 Registry-4, §9, §11.6).

A calendar invite is a ``VCALENDAR`` wrapping one ``VEVENT`` with an ``ORGANIZER``,
``ATTENDEE`` lines, a UTC ``DTSTART``/``DTEND`` window, and a stable ``UID`` — the
exact shape Outlook reads to place a meeting on every attendee's calendar. This
module renders that shape by hand from a small data model, **standard-library
only**, applying the two RFC 5545 serialization rules a hand-rolled writer must get
right or the file fails to import:

* **text escaping** — ``\\``, ``;``, ``,`` and newlines are backslash-escaped inside
  property *values* (``SUMMARY``/``DESCRIPTION``/``LOCATION``) so a comma in prose
  is not read as a value separator (§3.3.11);
* **line folding** — every content line is folded to ≤75 octets with a CRLF + space
  continuation, and all lines are CRLF-terminated (§3.1).

Output is deterministic for identical input (ids, timestamps supplied by the
caller) so a calendar renders byte-identically across runs (D10). Validate by
unfolding the lines and checking the ``BEGIN``/``END`` blocks pair and the required
properties are present.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = [
    "Attendee",
    "Calendar",
    "Meeting",
    "render_calendar",
]

#: Default product id stamped into ``PRODID`` (§3.7.3).
_PRODID = "-//enterprise_sim//outlook producer//EN"
#: Max octets per content line before folding (§3.1).
_FOLD_AT = 75


@dataclass(frozen=True, slots=True)
class Attendee:
    """A meeting participant (an ``ORGANIZER`` or ``ATTENDEE`` line).

    ``role`` is an RFC 5545 role token (``CHAIR`` for the organizer,
    ``REQ-PARTICIPANT`` / ``OPT-PARTICIPANT`` for invitees). ``rsvp`` sets the
    ``RSVP`` parameter so Outlook asks the attendee to respond.
    """

    name: str
    address: str
    role: str = "REQ-PARTICIPANT"
    rsvp: bool = True


@dataclass(frozen=True, slots=True)
class Meeting:
    """One ``VEVENT``: a scheduled meeting with organizer, attendees, and a window.

    ``uid`` is the event's stable id (durable across updates). ``start``/``end`` are
    the meeting window — converted to UTC ``…Z`` form on render. ``dtstamp`` is when
    the invite was created (defaults to ``start``). ``rrule`` optionally makes the
    event recurring (e.g. ``"FREQ=WEEKLY;COUNT=6"``). ``sequence`` bumps on each
    revision so a later send supersedes an earlier one.
    """

    uid: str
    summary: str
    start: datetime
    end: datetime
    organizer: Attendee
    attendees: Sequence[Attendee] = ()
    description: str = ""
    location: str = ""
    dtstamp: datetime | None = None
    sequence: int = 0
    status: str = "CONFIRMED"
    rrule: str | None = None


@dataclass(frozen=True, slots=True)
class Calendar:
    """A ``VCALENDAR`` wrapping one or more meetings.

    ``method`` is the iTIP method (``REQUEST`` for a new invite, ``CANCEL`` to
    withdraw one). ``prodid`` identifies the writer. Frozen so a rendered calendar
    is a stable snapshot.
    """

    meetings: Sequence[Meeting]
    method: str = "REQUEST"
    prodid: str = _PRODID
    extra_props: dict[str, str] = field(default_factory=dict)


# --- rendering --------------------------------------------------------------


def render_calendar(calendar: Calendar) -> str:
    """Render ``calendar`` to RFC 5545 ``.ics`` text (folded, CRLF-terminated).

    The result is a complete ``VCALENDAR`` with ``VERSION:2.0``, the supplied
    ``METHOD``, and one ``VEVENT`` per meeting. Every value is escaped and every
    line folded, so the output imports cleanly into Outlook / Google Calendar.
    """
    if not calendar.meetings:
        raise ValueError("a calendar needs at least one meeting")
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        _prop("PRODID", calendar.prodid),
        "CALSCALE:GREGORIAN",
        _prop("METHOD", calendar.method),
    ]
    for name, value in calendar.extra_props.items():
        lines.append(_prop(name, value))
    for meeting in calendar.meetings:
        lines.extend(_render_meeting(meeting))
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


def _render_meeting(meeting: Meeting) -> list[str]:
    """Render one meeting as the lines of a ``VEVENT`` block."""
    dtstamp = meeting.dtstamp or meeting.start
    lines = [
        "BEGIN:VEVENT",
        _prop("UID", meeting.uid),
        f"DTSTAMP:{_ics_datetime(dtstamp)}",
        f"DTSTART:{_ics_datetime(meeting.start)}",
        f"DTEND:{_ics_datetime(meeting.end)}",
        _prop("SUMMARY", meeting.summary),
    ]
    if meeting.description:
        lines.append(_prop("DESCRIPTION", meeting.description))
    if meeting.location:
        lines.append(_prop("LOCATION", meeting.location))
    lines.append(_organizer_line(meeting.organizer))
    lines.extend(_attendee_line(att) for att in meeting.attendees)
    if meeting.rrule:
        lines.append(f"RRULE:{meeting.rrule}")
    lines.append(f"SEQUENCE:{meeting.sequence}")
    lines.append(_prop("STATUS", meeting.status))
    lines.append("END:VEVENT")
    return lines


def _organizer_line(organizer: Attendee) -> str:
    """Render the ``ORGANIZER;CN=…:mailto:…`` line."""
    return f"ORGANIZER;CN={_param(organizer.name)}:mailto:{organizer.address}"


def _attendee_line(attendee: Attendee) -> str:
    """Render one ``ATTENDEE;CN=…;ROLE=…;RSVP=…:mailto:…`` line."""
    rsvp = "TRUE" if attendee.rsvp else "FALSE"
    return (
        f"ATTENDEE;CN={_param(attendee.name)};ROLE={attendee.role};"
        f"RSVP={rsvp};PARTSTAT=NEEDS-ACTION:mailto:{attendee.address}"
    )


# --- escaping / folding (§3.1, §3.3.11) -------------------------------------


def _prop(name: str, value: str) -> str:
    """A ``NAME:VALUE`` content line with the value text-escaped."""
    return f"{name}:{_escape_text(value)}"


def _escape_text(value: str) -> str:
    """Escape a TEXT value: backslash, semicolon, comma, and newlines (§3.3.11)."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _param(value: str) -> str:
    """Quote a parameter value (``CN``) when it contains a separator char (§3.2)."""
    if any(ch in value for ch in ":;,"):
        escaped = value.replace('"', "'")
        return f'"{escaped}"'
    return value


def _ics_datetime(value: datetime) -> str:
    """Format a datetime as UTC ``YYYYMMDDTHHMMSSZ`` (naive values treated as UTC)."""
    dt = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    """Fold a content line to ≤75 octets with CRLF + space continuations (§3.1).

    Folding is octet-based (UTF-8), and a continuation never splits a multi-byte
    character: bytes are accumulated up to the limit on character boundaries.
    """
    raw = line.encode("utf-8")
    if len(raw) <= _FOLD_AT:
        return line
    chunks: list[str] = []
    current = bytearray()
    for ch in line:
        encoded = ch.encode("utf-8")
        # First chunk gets the full width; continuations reserve one octet for the space.
        limit = _FOLD_AT if not chunks else _FOLD_AT - 1
        if len(current) + len(encoded) > limit:
            chunks.append(current.decode("utf-8"))
            current = bytearray()
        current.extend(encoded)
    chunks.append(current.decode("utf-8"))
    return "\r\n ".join(chunks)
