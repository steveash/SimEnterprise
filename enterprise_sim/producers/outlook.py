"""The ``outlook`` producer — events → grounded ``.eml`` threads + ``.ics`` invites.

The second concrete producer (after ``markdown``, §16): it satisfies the same
:class:`~enterprise_sim.core.registry.plugins.Producer` protocol and renders the
*same* format-agnostic :class:`~enterprise_sim.core.events.Event` into Outlook's two
native artifacts, choosing by the event's abstract deliverable:

* an **e-mail thread** (``.eml``) for ``email_thread`` / ``email`` deliverables —
  a real RFC 5322 conversation linked by ``Message-ID`` / ``In-Reply-To`` /
  ``References`` with quoted reply history (:mod:`enterprise_sim.producers.email_eml`);
* a **calendar invite** (``.ics``) for ``meeting_invite`` / ``calendar_invite``
  deliverables — a RFC 5545 ``VEVENT`` with organizer, attendees, and a UTC window
  (:mod:`enterprise_sim.producers.calendar_ics`).

All three grounding layers carry over from the markdown producer (D30): a
:class:`~enterprise_sim.producers.grounding.Roster` constrains what the model may
name; the participant mailboxes, subject, dates, and citations are *templated* from
the event's bound roles and verified references, never generated; and a single
detect-and-repair pass turns any surviving out-of-scope name into a soft validation
issue while keeping the artifact (§16.2.3 / D17). The producer is a pure function of
``(event, view)`` — deterministic given a deterministic backend.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from enterprise_sim.core.events import Event
from enterprise_sim.core.llm import LLMClient, Prompt, assemble_prompt
from enterprise_sim.core.world import Edge, Node, WorldView
from enterprise_sim.producers.artifact import Mention, ProducedArtifact, ValidationIssue
from enterprise_sim.producers.calendar_ics import Attendee, Calendar, Meeting, render_calendar
from enterprise_sim.producers.email_eml import (
    EmailMessage,
    EmailThread,
    Participant,
    render_thread,
)
from enterprise_sim.producers.grounding import Roster, detect_unresolved_names, tag_mentions
from enterprise_sim.producers.markdown import ProducerContext

__all__ = ["OutlookProducer"]

# KG node/edge type constants this producer reads and writes (§3).
N_ARTIFACT = "Artifact"
N_COMPANY = "Company"
E_AUTHORED = "authored"
E_REVIEWED = "reviewed"
E_ATTENDED = "attended"
E_EXPRESSES = "expresses"
E_REFERENCES = "references"

# Roles in ``event.actors`` that map to the sender/organizer vs the recipients.
_SENDER_ROLES = ("author", "authors", "organizer", "sender", "from")
_PARTICIPANT_ROLES = (
    "reviewer",
    "reviewers",
    "recipient",
    "recipients",
    "attendee",
    "attendees",
    "participants",
    "to",
    "cc",
)

# Abstract deliverable kinds (and media) routed to each rendering mode.
_EMAIL_KINDS = frozenset({"email_thread", "email", "announcement", "message"})
_CALENDAR_KINDS = frozenset({"meeting_invite", "calendar_invite", "meeting", "invite", "calendar"})
_EMAIL_MEDIA = frozenset({"email", "eml", "mail"})
_CALENDAR_MEDIA = frozenset({"calendar", "ics", "icalendar"})

#: Default duration of a rendered meeting when the event names no end.
_MEETING_DURATION = timedelta(hours=1)
#: Deterministic offset of a reply from the message it answers.
_REPLY_DELAY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class _Mailbox:
    """A resolved participant: the KG node plus its derived mailbox."""

    node: Node
    name: str
    address: str


class OutlookProducer:
    """Render an :class:`Event` to an Outlook ``.eml`` thread or ``.ics`` invite.

    Satisfies the :class:`~enterprise_sim.core.registry.plugins.Producer` protocol
    (``name`` / ``formats`` / ``handles``). The binding map routes the email- and
    meeting-shaped deliverable kinds here; everything else stays with ``markdown``.
    """

    name = "outlook"
    formats: Sequence[str] = ("eml", "ics")
    handles: Sequence[str] = (
        "email_thread",
        "email",
        "announcement",
        "meeting_invite",
        "calendar_invite",
        "meeting",
    )

    def produce(
        self,
        event: Event,
        view: WorldView,
        client: LLMClient,
        ctx: ProducerContext | None = None,
    ) -> ProducedArtifact:
        """Render ``event`` against ``view`` and return its :class:`ProducedArtifact`.

        Nothing is written to disk and ``view`` is not mutated — the caller applies
        the result via :func:`~enterprise_sim.producers.artifact.apply_to_world`.
        """
        ctx = ctx or ProducerContext()
        roster = Roster.from_worldview(view)
        domain = _company_domain(view)

        kind = event.deliverable.kind if event.deliverable else "email_thread"
        medium = event.deliverable.medium if event.deliverable else "email"
        as_calendar = _is_calendar(kind, medium)
        title = _title_for(event, kind)
        artifact_id = _artifact_id_for(event)
        fmt = "ics" if as_calendar else "eml"
        path = f"{ctx.artifacts_dir}/{_slug(artifact_id)}.{fmt}"

        sender = _resolve_mailboxes(event, view, _SENDER_ROLES, domain)
        participants = _resolve_mailboxes(event, view, _PARTICIPANT_ROLES, domain)
        # Fall back so a one-sided actor map still yields a believable exchange.
        if not sender and participants:
            sender, participants = participants[:1], participants[1:]
        candidates = _candidate_references(view, exclude=artifact_id)

        prose, references, issues = self._generate_grounded(
            client=client,
            event=event,
            kind=kind,
            title=title,
            roster=roster,
            sender=sender,
            participants=participants,
            candidates=candidates,
            ctx=ctx,
            path=path,
            as_calendar=as_calendar,
        )

        if as_calendar:
            body = _render_invite(
                event=event,
                title=title,
                uid=f"{_slug(artifact_id)}@{domain}",
                organizer=sender[0] if sender else None,
                attendees=participants,
                description=prose,
            )
        else:
            body = _render_thread(
                event=event,
                subject=title,
                sender=sender[0] if sender else None,
                recipients=participants,
                prose=prose,
                domain=domain,
                artifact_id=artifact_id,
            )

        mentions = tag_mentions(body, roster, artifact_path=path)
        node = _artifact_node(
            artifact_id=artifact_id,
            kind=kind,
            medium="calendar" if as_calendar else "email",
            title=title,
            event=event,
            sender=sender,
            participants=participants,
            path=path,
        )
        edges = _build_edges(
            artifact_id=artifact_id,
            event=event,
            view=view,
            sender=sender,
            participants=participants,
            references=references,
            as_calendar=as_calendar,
        )
        metadata = _metadata(
            artifact_id=artifact_id,
            kind=kind,
            medium="calendar" if as_calendar else "email",
            fmt=fmt,
            title=title,
            path=path,
            event=event,
            sender=sender,
            participants=participants,
            references=references,
            mentions=mentions,
            issues=issues,
        )
        return ProducedArtifact(
            artifact_id=artifact_id,
            path=path,
            fmt=fmt,
            body=body,
            node=node,
            edges=edges,
            mentions=mentions,
            issues=issues,
            metadata=metadata,
        )

    # -- generation + grounding repair ------------------------------------

    def _generate_grounded(
        self,
        *,
        client: LLMClient,
        event: Event,
        kind: str,
        title: str,
        roster: Roster,
        sender: list[_Mailbox],
        participants: list[_Mailbox],
        candidates: list[str],
        ctx: ProducerContext,
        path: str,
        as_calendar: bool,
    ) -> tuple[str, tuple[str, ...], list[ValidationIssue]]:
        """Generate the message/agenda prose, then detect + (at most one) repair.

        Mirrors the markdown producer's single-repair loop (D30.3): a draft that
        names an out-of-scope entity is regenerated once with a correction; anything
        still unresolved becomes a validation issue and the artifact is kept.
        """
        system = _system_prompt(kind, as_calendar=as_calendar)
        stable = _stable_context(ctx)
        brief = _task_brief(
            event=event,
            kind=kind,
            title=title,
            roster=roster,
            sender=sender,
            participants=participants,
            candidates=candidates,
            as_calendar=as_calendar,
        )
        prompt = assemble_prompt(system=system, stable_context=stable, brief=brief)
        result = client.generate_content(prompt, candidate_references=candidates)
        prose = result.content
        references = result.references_used

        unresolved = detect_unresolved_names(prose, roster)
        if unresolved:
            repaired = client.generate_content(
                _repair_prompt(system, stable, brief, unresolved),
                candidate_references=candidates,
            )
            prose = repaired.content
            references = repaired.references_used
            unresolved = detect_unresolved_names(prose, roster)

        issues: list[ValidationIssue] = []
        if unresolved:
            issues.append(
                ValidationIssue(
                    kind="unresolved_mention",
                    message=(
                        "names not resolvable to an in-scope entity survived one "
                        f"repair pass: {', '.join(unresolved)}"
                    ),
                    where=path,
                    details={"names": list(unresolved)},
                )
            )
        return prose, references, issues


# -- prompt assembly --------------------------------------------------------


def _system_prompt(kind: str, *, as_calendar: bool) -> str:
    """Per-deliverable-kind system prompt (cacheable across the run, §16.1)."""
    if as_calendar:
        return (
            "You write the agenda body of a meeting invite for an enterprise "
            "knowledge corpus. Write a short, factual agenda grounded strictly in "
            "the supplied context. Refer only to the people and entities named in "
            "the roster, by exactly those names. Do not invent names, dates, or facts."
        )
    human = kind.replace("_", " ")
    return (
        f"You write the body of a business {human} for an enterprise knowledge "
        "corpus. Write a clear, factual e-mail grounded strictly in the supplied "
        "context. Refer only to the people and entities named in the roster, by "
        "exactly those names. Do not invent names, dates, or facts."
    )


def _stable_context(ctx: ProducerContext) -> list[str]:
    blocks: list[str] = []
    if ctx.company_profile:
        blocks.append(ctx.company_profile)
    if ctx.scenario_context:
        blocks.append(ctx.scenario_context)
    return blocks


def _task_brief(
    *,
    event: Event,
    kind: str,
    title: str,
    roster: Roster,
    sender: list[_Mailbox],
    participants: list[_Mailbox],
    candidates: Sequence[str],
    as_calendar: bool,
) -> str:
    """The volatile per-artifact suffix: brief + roster + templated facts (§16.1)."""
    topic = str(event.payload.get("topic") or event.payload.get("intent") or title)
    what = "agenda body of a meeting invite" if as_calendar else "body of an e-mail"
    lines = [
        f"Task: write the {what}.",
        f"Subject: {title}.",
        f"Date: {event.timestamp.date().isoformat()}.",
        f"Topic: {topic}.",
    ]
    if sender:
        lines.append(f"{'Organized' if as_calendar else 'Sent'} by: {_mb_names(sender)}.")
    if participants:
        label = "Attendees" if as_calendar else "Recipients"
        lines.append(f"{label}: {_mb_names(participants)}.")
    lines.append("")
    lines.append(roster.roster_block())
    if candidates:
        lines.append("")
        lines.append(
            "You may cite any of these prior artifacts by id, and you MUST report "
            "the ids you cite in references_used: " + ", ".join(candidates)
        )
    return "\n".join(lines)


def _repair_prompt(
    system: str,
    stable: list[str],
    brief: str,
    unresolved: Sequence[str],
) -> Prompt:
    """Re-assemble the prompt with a corrective instruction (the single repair, D30.3)."""
    correction = (
        "\n\nCORRECTION: your previous draft named "
        f"{', '.join(unresolved)}, which is not in scope. Rewrite using ONLY the "
        "roster names above; remove or replace every other name."
    )
    return assemble_prompt(system=system, stable_context=stable, brief=brief + correction)


# -- email / calendar rendering ---------------------------------------------


def _render_thread(
    *,
    event: Event,
    subject: str,
    sender: _Mailbox | None,
    recipients: list[_Mailbox],
    prose: str,
    domain: str,
    artifact_id: str,
) -> str:
    """Build a grounded e-mail thread and render it to ``.eml`` text.

    The opening message carries the generated prose; if there is a recipient, a
    single templated acknowledgement reply (grounded by construction — it names
    only roster people) turns the artifact into a real two-message conversation.
    """
    if sender is None:
        sender = recipients[0] if recipients else _fallback_mailbox(domain)
        recipients = recipients[1:]
    base = _slug(artifact_id)
    to = [_participant(m) for m in recipients] or [_participant(sender)]
    opening = EmailMessage(
        message_id=f"{base}.0@{domain}",
        sender=_participant(sender),
        to=to,
        subject=subject,
        date=event.timestamp,
        body=prose.strip() or f"Sharing the {subject} for your review.",
    )
    messages = [opening]
    if recipients:
        replier = recipients[0]
        messages.append(
            EmailMessage(
                message_id=f"{base}.1@{domain}",
                sender=_participant(replier),
                to=[_participant(sender)],
                cc=[_participant(m) for m in recipients[1:]],
                subject=_reply_subject(subject),
                date=event.timestamp + _REPLY_DELAY,
                body=f"Thanks, {sender.name}. Reviewed — this looks good to me.",
                in_reply_to=opening.message_id,
                references=[opening.message_id],
            )
        )
    thread = EmailThread(messages=messages, domain=domain)
    return render_thread(thread).decode("utf-8")


def _render_invite(
    *,
    event: Event,
    title: str,
    uid: str,
    organizer: _Mailbox | None,
    attendees: list[_Mailbox],
    description: str,
) -> str:
    """Build a grounded calendar invite and render it to ``.ics`` text."""
    organizer = organizer or (attendees[0] if attendees else _fallback_mailbox_node())
    start = event.timestamp
    end = start + _MEETING_DURATION
    location = str(event.payload.get("location") or "")
    meeting = Meeting(
        uid=uid,
        summary=title,
        start=start,
        end=end,
        organizer=Attendee(name=organizer.name, address=organizer.address, role="CHAIR"),
        attendees=tuple(
            Attendee(name=m.name, address=m.address, role="REQ-PARTICIPANT") for m in attendees
        ),
        description=description.strip(),
        location=location,
        dtstamp=start,
    )
    return render_calendar(Calendar(meetings=[meeting]))


def _participant(mailbox: _Mailbox) -> Participant:
    return Participant(name=mailbox.name, address=mailbox.address)


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


# -- KG node/edge construction ----------------------------------------------


def _artifact_node(
    *,
    artifact_id: str,
    kind: str,
    medium: str,
    title: str,
    event: Event,
    sender: list[_Mailbox],
    participants: list[_Mailbox],
    path: str,
) -> Node:
    """Build (or re-declare) the ``Artifact`` node this file renders (§3)."""
    return Node(
        id=artifact_id,
        type=N_ARTIFACT,
        created_at=event.timestamp,
        props={
            "kind": kind,
            "medium": medium,
            "title": title,
            "path": path,
            "date": event.timestamp.date().isoformat(),
            "authors": [m.node.id for m in sender],
            "participants": [m.node.id for m in participants],
            "event": event.id,
        },
        aliases=[title],
    )


def _build_edges(
    *,
    artifact_id: str,
    event: Event,
    view: WorldView,
    sender: list[_Mailbox],
    participants: list[_Mailbox],
    references: Sequence[str],
    as_calendar: bool,
) -> list[Edge]:
    """Build the reified relationships the artifact expresses (§3, §11.3, D16).

    The sender/organizer ``authored`` the artifact; recipients ``reviewed`` an
    e-mail while attendees ``attended`` a meeting (the §3 edge vocabulary). The
    artifact ``expresses`` its in-scope subjects and ``references`` verified cites.
    """
    edges: list[Edge] = []
    ts = event.timestamp
    participant_edge = E_ATTENDED if as_calendar else E_REVIEWED
    for author in sender:
        edges.append(_edge(E_AUTHORED, author.node.id, artifact_id, ts))
    for participant in participants:
        edges.append(_edge(participant_edge, participant.node.id, artifact_id, ts))
    for subject in event.subjects:
        if subject == artifact_id or view.get_node(subject) is None:
            continue
        edges.append(_edge(E_EXPRESSES, artifact_id, subject, ts))
    for ref in references:
        edges.append(_edge(E_REFERENCES, artifact_id, ref, ts))
    return edges


def _edge(edge_type: str, src: str, dst: str, ts: datetime) -> Edge:
    return Edge(
        id=f"edge:{edge_type}:{src}->{dst}",
        type=edge_type,
        src=src,
        dst=dst,
        created_at=ts,
    )


def _metadata(
    *,
    artifact_id: str,
    kind: str,
    medium: str,
    fmt: str,
    title: str,
    path: str,
    event: Event,
    sender: list[_Mailbox],
    participants: list[_Mailbox],
    references: Sequence[str],
    mentions: Sequence[Mention],
    issues: Sequence[ValidationIssue],
) -> dict[str, object]:
    """The JSON twin of the artifact (the side-car metadata deliverable)."""
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "medium": medium,
        "format": fmt,
        "title": title,
        "path": path,
        "event": event.id,
        "date": event.timestamp.date().isoformat(),
        "from": [m.node.id for m in sender],
        "participants": [m.node.id for m in participants],
        "references": list(references),
        "mentions": [m.to_dict() for m in mentions],
        "issues": [i.to_dict() for i in issues],
    }


# -- small helpers ----------------------------------------------------------


def _is_calendar(kind: str, medium: str) -> bool:
    """Decide whether ``event`` renders as a calendar invite vs an e-mail thread."""
    if kind in _CALENDAR_KINDS or medium in _CALENDAR_MEDIA:
        return True
    if kind in _EMAIL_KINDS or medium in _EMAIL_MEDIA:
        return False
    # Unknown kind: a "meeting"-ish token anywhere tips to calendar, else e-mail.
    return "meeting" in kind or "calendar" in kind


def _resolve_mailboxes(
    event: Event, view: WorldView, roles: Sequence[str], domain: str
) -> list[_Mailbox]:
    """Resolve the in-scope person nodes bound to ``roles`` into mailboxes.

    Ids are de-duplicated preserving first-seen order; an id absent from the view
    is skipped (a producer can only name in-scope people), so a templated address
    never references a phantom person.
    """
    seen: set[str] = set()
    mailboxes: list[_Mailbox] = []
    for role in roles:
        for pid in event.actors.get(role, []):
            if pid in seen:
                continue
            seen.add(pid)
            node = view.get_node(pid)
            if node is not None:
                mailboxes.append(_mailbox_for(node, domain))
    return mailboxes


def _mailbox_for(node: Node, domain: str) -> _Mailbox:
    """Derive a mailbox for a person node: explicit ``email`` prop, else name@domain."""
    name = _display_name(node)
    explicit = node.props.get("email")
    if isinstance(explicit, str) and "@" in explicit:
        return _Mailbox(node=node, name=name, address=explicit)
    local = _slug(name).replace("-", ".") or _slug(node.id) or "person"
    return _Mailbox(node=node, name=name, address=f"{local}@{domain}")


def _fallback_mailbox(domain: str) -> _Mailbox:
    """A placeholder sender for the degenerate no-actor case (keeps output valid)."""
    node = Node(
        id="person:unknown", type="Person", created_at=datetime.min, props={"name": "Sender"}
    )
    return _Mailbox(node=node, name="Sender", address=f"sender@{domain}")


def _fallback_mailbox_node() -> _Mailbox:
    return _fallback_mailbox("example.com")


def _company_domain(view: WorldView) -> str:
    """The e-mail domain for the run's company: explicit prop, else slug of its name."""
    companies = view.nodes_by_type(N_COMPANY)
    if not companies:
        return "example.com"
    node = companies[0]
    explicit = node.props.get("domain")
    if isinstance(explicit, str) and explicit:
        return explicit
    name = node.props.get("name")
    slug = _slug(str(name)) if name else ""
    return f"{slug}.com" if slug else "example.com"


def _candidate_references(view: WorldView, *, exclude: str) -> list[str]:
    """Recent in-scope artifact ids the model may cite (the candidate set, §16.3)."""
    return [n.id for n in view.nodes_by_type(N_ARTIFACT) if n.id != exclude]


def _mb_names(mailboxes: Sequence[_Mailbox]) -> str:
    return ", ".join(m.name for m in mailboxes)


def _display_name(node: Node) -> str:
    name = node.props.get("name") or node.props.get("title")
    return str(name) if name else node.id.split(":", 1)[-1]


def _title_for(event: Event, kind: str) -> str:
    title = event.payload.get("title")
    if isinstance(title, str) and title:
        return title
    topic = event.payload.get("topic")
    human = kind.replace("_", " ").title()
    if isinstance(topic, str) and topic:
        return f"{human}: {topic}"
    return human


def _artifact_id_for(event: Event) -> str:
    """Use an existing abstract-artifact subject if present, else mint from the event."""
    for subject in event.subjects:
        if subject.startswith(("artifact:", "art:")):
            return subject
    return f"artifact:{event.id.split(':', 1)[-1]}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "artifact"
