"""Tests for the outlook producer (ARCHITECTURE.md §4 Registry-4, §9, §16).

Acceptance (esim-e10fea9e): the producer renders an e-mail thread (``.eml``) and a
calendar invite (``.ics``) that **validate** — the ``.eml`` round-trips through the
stdlib :mod:`email` parser with a real threaded header chain, and the ``.ics`` is a
well-formed RFC 5545 ``VCALENDAR``/``VEVENT``. Also covers grounding (templated
mailboxes + roster), reference + relationship edges, mentions, determinism, and the
single detect-and-repair pass shared with the markdown producer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email import message_from_bytes, message_from_string
from email.policy import default as default_policy
from typing import Any

from enterprise_sim.core.events import Deliverable, Event
from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.llm.backends import estimate_tokens
from enterprise_sim.core.llm.prompt import Prompt
from enterprise_sim.core.llm.types import Completion, TokenUsage
from enterprise_sim.core.world import Node, World
from enterprise_sim.producers import (
    OutlookProducer,
    ProducerContext,
    apply_to_world,
    mention_records,
)
from enterprise_sim.producers.calendar_ics import (
    Attendee,
    Calendar,
    Meeting,
    render_calendar,
)
from enterprise_sim.producers.email_eml import (
    EmailMessage,
    EmailThread,
    Participant,
    render_thread,
)

_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_T_EVENT = datetime(2026, 6, 12, 14, 0, tzinfo=UTC)


def _person(node_id: str, name: str, *, email: str | None = None) -> Node:
    props: dict[str, Any] = {"name": name}
    if email:
        props["email"] = email
    return Node(node_id, "Person", _T0, props=props)


def _artifact(node_id: str, title: str) -> Node:
    return Node(node_id, "Artifact", _T0, props={"title": title, "kind": "status_report"})


def _world() -> World:
    world = World()
    world.add_node(_person("person:ada", "Ada Lovelace", email="ada@acme.example"))
    world.add_node(_person("person:alan", "Alan Turing"))
    world.add_node(
        Node("company:acme", "Company", _T0, props={"name": "Acme", "domain": "acme.example"})
    )
    world.add_node(Node("project:payments", "Project", _T0, props={"name": "Payments Platform"}))
    world.add_node(_artifact("artifact:status-w11", "Status Week 11"))
    world.add_node(_artifact("artifact:design-pay", "Payments Design"))
    return world


def _email_event() -> Event:
    return Event(
        id="evt:rollout-email",
        type="EmailSent",
        timestamp=_T_EVENT,
        actors={"author": ["person:ada"], "reviewers": ["person:alan"]},
        project="project:payments",
        subjects=["project:payments"],
        deliverable=Deliverable(kind="email_thread", medium="email"),
        payload={"topic": "payments rollout", "title": "payments rollout update"},
    )


def _meeting_event() -> Event:
    return Event(
        id="evt:rollout-meeting",
        type="MeetingScheduled",
        timestamp=_T_EVENT,
        actors={"organizer": ["person:ada"], "attendees": ["person:alan"]},
        project="project:payments",
        subjects=["project:payments"],
        deliverable=Deliverable(kind="meeting_invite", medium="calendar"),
        payload={
            "topic": "payments rollout",
            "title": "payments rollout sync",
            "location": "Room 1, HQ",
        },
    )


def _fake_client() -> LLMClient:
    from enterprise_sim.core.llm import LLMConfig

    return LLMClient.from_config(LLMConfig(backend="fake", cache_enabled=False))


# -- e-mail thread (.eml) ---------------------------------------------------


def test_email_renders_validating_eml() -> None:
    produced = OutlookProducer().produce(_email_event(), _world(), _fake_client())
    assert produced.fmt == "eml"
    assert produced.path == "artifacts/artifact-rollout-email.eml"
    # The artifact body is a parseable RFC 5322 message (it "validates").
    msg = message_from_bytes(produced.body.encode("utf-8"), policy=default_policy)
    assert msg["Subject"].startswith("Re: ")  # the reply tops the thread
    assert "ada@acme.example" in msg["To"]  # reply is addressed back to the sender
    assert "alan.turing@acme.example" in msg["From"]  # derived mailbox for Alan
    assert msg["Message-ID"] and msg["In-Reply-To"] and msg["References"]


def test_email_threads_messages_with_headers() -> None:
    produced = OutlookProducer().produce(_email_event(), _world(), _fake_client())
    msg = message_from_string(produced.body, policy=default_policy)
    # In-Reply-To names the opening message; References carries the chain.
    assert msg["In-Reply-To"] == "<artifact-rollout-email.0@acme.example>"
    assert "<artifact-rollout-email.0@acme.example>" in msg["References"]
    # The quoted history of the opening message is inlined in the reply body.
    body = msg.get_content()
    assert "wrote:" in body
    assert "> " in body


def test_email_node_and_relationship_edges() -> None:
    produced = OutlookProducer().produce(_email_event(), _world(), _fake_client())
    assert produced.node.type == "Artifact"
    assert produced.node.props["medium"] == "email"
    by_type: dict[str, list[Any]] = {}
    for edge in produced.edges:
        by_type.setdefault(edge.type, []).append(edge)
    assert any(e.src == "person:ada" for e in by_type["authored"])
    assert any(e.src == "person:alan" for e in by_type["reviewed"])  # recipient reviews
    assert any(e.type == "expresses" and e.dst == "project:payments" for e in produced.edges)


def test_email_records_mentions_and_references() -> None:
    produced = OutlookProducer().produce(_email_event(), _world(), _fake_client())
    entities = {m.entity_id for m in produced.mentions}
    assert "person:ada" in entities
    assert "person:alan" in entities
    for mention in produced.mentions:
        span = produced.body[
            mention.locator.offset : mention.locator.offset + mention.locator.length
        ]
        assert span == mention.surface_form
    # Any reference edge points only at a verified candidate (the fake cites a subset).
    for edge in (e for e in produced.edges if e.type == "references"):
        assert edge.dst in {"artifact:status-w11", "artifact:design-pay"}


def test_email_creates_reference_edges_from_verified_cites() -> None:
    # A backend that claims a real + a hallucinated cite: only the real one survives.
    backend = _CitingBackend(
        body="Ada Lovelace shared the rollout plan.",
        cites=("artifact:status-w11", "artifact:hallucinated"),
    )
    produced = OutlookProducer().produce(_email_event(), _world(), _client_with(backend))
    ref_edges = [e for e in produced.edges if e.type == "references"]
    assert [e.dst for e in ref_edges] == ["artifact:status-w11"]
    assert produced.metadata["references"] == ["artifact:status-w11"]


# -- calendar invite (.ics) -------------------------------------------------


def test_calendar_renders_validating_ics() -> None:
    produced = OutlookProducer().produce(_meeting_event(), _world(), _fake_client())
    assert produced.fmt == "ics"
    assert produced.path == "artifacts/artifact-rollout-meeting.ics"
    text = produced.body
    # Structural validity: blocks pair and the required properties are present.
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.rstrip().endswith("END:VCALENDAR")
    assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT") == 1
    for required in ("VERSION:2.0", "UID:", "DTSTAMP:", "DTSTART:", "DTEND:", "SUMMARY:"):
        assert required in text
    # Organizer + attendee are templated from the bound roles.
    assert "ORGANIZER;CN=Ada Lovelace:mailto:ada@acme.example" in text
    assert "mailto:alan.turing@acme.example" in text
    assert "LOCATION:Room 1\\, HQ" in text  # comma escaped per RFC 5545


def test_calendar_dtstart_is_utc_and_window_is_one_hour() -> None:
    produced = OutlookProducer().produce(_meeting_event(), _world(), _fake_client())
    lines = _unfold(produced.body)
    dtstart = next(line for line in lines if line.startswith("DTSTART:"))
    dtend = next(line for line in lines if line.startswith("DTEND:"))
    assert dtstart == "DTSTART:20260612T140000Z"
    assert dtend == "DTEND:20260612T150000Z"


def test_calendar_attended_edges() -> None:
    produced = OutlookProducer().produce(_meeting_event(), _world(), _fake_client())
    assert produced.node.props["medium"] == "calendar"
    attended = [e for e in produced.edges if e.type == "attended"]
    assert any(e.src == "person:alan" and e.dst == produced.artifact_id for e in attended)
    authored = [e for e in produced.edges if e.type == "authored"]
    assert any(e.src == "person:ada" for e in authored)


def test_calendar_lines_are_folded_to_75_octets() -> None:
    # A long description forces line folding; every physical line must be ≤75 octets.
    world = _world()
    event = _meeting_event()
    event.payload["title"] = "payments rollout sync"
    produced = OutlookProducer().produce(event, world, _fake_client())
    for physical_line in produced.body.split("\r\n"):
        assert len(physical_line.encode("utf-8")) <= 75


# -- determinism + KG application -------------------------------------------


def test_produce_is_deterministic() -> None:
    a = OutlookProducer().produce(_email_event(), _world(), _fake_client())
    b = OutlookProducer().produce(_email_event(), _world(), _fake_client())
    assert a.body == b.body
    assert [e.id for e in a.edges] == [e.id for e in b.edges]
    assert [m.to_dict() for m in a.mentions] == [m.to_dict() for m in b.mentions]


def test_apply_to_world_and_side_files() -> None:
    world = _world()
    produced = OutlookProducer().produce(_meeting_event(), world, _fake_client())
    apply_to_world(world, [produced])
    assert world.get_node(produced.artifact_id) is not None
    for edge in produced.edges:
        assert world.get_edge(edge.id) is not None
    apply_to_world(world, [produced])  # idempotent
    assert mention_records([produced]) == [m.to_dict() for m in produced.mentions]


def test_uses_existing_abstract_artifact_subject() -> None:
    event = _email_event()
    event.subjects = ["artifact:welcome-email", "project:payments"]
    produced = OutlookProducer().produce(event, _world(), _fake_client())
    assert produced.artifact_id == "artifact:welcome-email"


# -- detect + single repair (shared with markdown, §16.2.3 / D17) -----------


class _ScriptedBackend:
    """A backend whose ``generate_content`` returns queued bodies in order."""

    name = "scripted"

    def __init__(self, bodies: Sequence[str]) -> None:
        self._bodies = list(bodies)
        self.calls = 0
        self.prompts: list[Prompt] = []

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:  # pragma: no cover - unused
        raise NotImplementedError

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        self.prompts.append(prompt)
        body = self._bodies[min(self.calls, len(self._bodies) - 1)]
        self.calls += 1
        return Completion(
            text=body, usage=TokenUsage(output_tokens=estimate_tokens(body)), model=model
        )


class _CitingBackend:
    """A backend that returns one clean body claiming a fixed set of citations."""

    name = "citing"

    def __init__(self, *, body: str, cites: Sequence[str]) -> None:
        self._body = body
        self._cites = tuple(cites)

    def generate_structured(
        self, prompt: Prompt, *, schema: Mapping[str, Any], model: str, temperature: float
    ) -> Completion:  # pragma: no cover - unused
        raise NotImplementedError

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        usage = TokenUsage(output_tokens=estimate_tokens(self._body))
        return Completion(text=self._body, usage=usage, model=model, references_used=self._cites)


def _client_with(backend: _ScriptedBackend | _CitingBackend) -> LLMClient:
    from enterprise_sim.core.llm import LLMConfig

    return LLMClient(backend, config=LLMConfig(backend="scripted", cache_enabled=False))


def test_repair_pass_fixes_unresolved_name() -> None:
    backend = _ScriptedBackend(
        [
            "Ada Lovelace met Grace Hopper about the rollout.",  # hallucinated name
            "Ada Lovelace shared the rollout plan with the team.",  # clean rewrite
        ]
    )
    produced = OutlookProducer().produce(_email_event(), _world(), _client_with(backend))
    assert backend.calls == 2
    assert produced.issues == []
    assert "Grace Hopper" not in produced.body


def test_unrepaired_name_becomes_validation_issue() -> None:
    backend = _ScriptedBackend(["Ada Lovelace and Grace Hopper shipped it."])  # always dirty
    produced = OutlookProducer().produce(_email_event(), _world(), _client_with(backend))
    assert backend.calls == 2
    assert len(produced.issues) == 1
    assert produced.issues[0].kind == "unresolved_mention"
    assert "Grace Hopper" in produced.issues[0].details["names"]


def test_context_feeds_prompt_and_directory() -> None:
    backend = _ScriptedBackend(["Ada Lovelace led the rollout."])
    ctx = ProducerContext(
        company_profile="Acme — a payments company.",
        scenario_context="Scenario: payments platform rollout.",
        artifacts_dir="corpus/mail",
    )
    produced = OutlookProducer().produce(_email_event(), _world(), _client_with(backend), ctx)
    assert produced.path.startswith("corpus/mail/")
    prompt_text = backend.prompts[0].text
    assert "Acme — a payments company." in prompt_text
    assert "refer ONLY to the following" in prompt_text
    assert "Ada Lovelace" in prompt_text


# -- the underlying renderers in isolation ----------------------------------


def test_render_thread_roundtrips_and_threads() -> None:
    opening = EmailMessage(
        message_id="m0@x.test",
        sender=Participant("Ada Lovelace", "ada@x.test"),
        to=[Participant("Alan Turing", "alan@x.test")],
        subject="rollout",
        date=_T_EVENT,
        body="First message.",
    )
    reply = EmailMessage(
        message_id="m1@x.test",
        sender=Participant("Alan Turing", "alan@x.test"),
        to=[Participant("Ada Lovelace", "ada@x.test")],
        subject="Re: rollout",
        date=_T_EVENT,
        body="Reply message.",
        in_reply_to="m0@x.test",
        references=["m0@x.test"],
    )
    msg = message_from_bytes(render_thread(EmailThread([opening, reply])), policy=default_policy)
    assert msg["Message-ID"] == "<m1@x.test>"
    assert msg["In-Reply-To"] == "<m0@x.test>"
    assert "First message." in msg.get_content()  # quoted history present


def test_render_calendar_escapes_and_pairs_blocks() -> None:
    meeting = Meeting(
        uid="u1@x.test",
        summary="Sync; review",
        start=_T_EVENT,
        end=_T_EVENT,
        organizer=Attendee("Ada Lovelace", "ada@x.test", role="CHAIR"),
        attendees=[Attendee("Alan Turing", "alan@x.test")],
        description="Discuss A, B, and C.",
    )
    text = render_calendar(Calendar([meeting]))
    assert "SUMMARY:Sync\\; review" in text  # semicolon escaped
    assert "DESCRIPTION:Discuss A\\, B\\, and C." in text  # commas escaped
    assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT") == 1


def _unfold(ics: str) -> list[str]:
    """Reverse RFC 5545 line folding into logical content lines."""
    lines: list[str] = []
    for raw in ics.split("\r\n"):
        if raw.startswith(" ") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines
