"""Tests for the jira producer + the multi-modal fan-out (ARCHITECTURE.md §4, §16, D6).

Acceptance (esim-0710d385): a ``jira`` producer renders events to Jira-style issues
(JSON, ndjson-aggregable), and the binding map fans one ``backlog`` event out to
both ``markdown`` and ``jira`` — proving the registry's one-to-many, cross-modally
KG-consistent fan-out. We confirm the issue's JSON shape + grounded fields, the KG
node/edges, jira-medium mentions whose locators slice the body, determinism, and
the fan-out binding.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from enterprise_sim.assembly.corpus import _producer_for, _producers_for
from enterprise_sim.core.events import Deliverable, Event
from enterprise_sim.core.llm import LLMClient, LLMConfig
from enterprise_sim.core.world import Node, World
from enterprise_sim.producers import JiraProducer, MarkdownProducer
from enterprise_sim.producers.artifact import apply_to_world

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
    return world


def _event(*, kind: str = "backlog", reviewers: list[str] | None = None) -> Event:
    return Event(
        id="evt:backlog-w12",
        type="BacklogGroomed",
        timestamp=_T_EVENT,
        actors={
            "lead": ["person:ada"],
            "engineers": ["person:alan"],
            "reviewers": ["person:grace"] if reviewers is None else reviewers,
        },
        initiative="init:payments",
        project="project:payments",
        subjects=["project:payments"],
        deliverable=Deliverable(kind=kind, medium="document"),
        payload={"topic": "payments rollout", "intent": "groom the backlog"},
    )


def _client() -> LLMClient:
    return LLMClient.from_config(LLMConfig(backend="fake", cache_enabled=False))


def _issue(event: Event | None = None) -> dict[str, Any]:
    produced = JiraProducer().produce(
        event or _event(), _world().projection(at=_T_EVENT), _client()
    )
    issue: dict[str, Any] = json.loads(produced.body)
    return issue


# -- end-to-end producer ----------------------------------------------------


def test_produce_emits_valid_jira_issue_json() -> None:
    produced = JiraProducer().produce(_event(), _world().projection(at=_T_EVENT), _client())
    assert produced.fmt == "jira"
    assert produced.path.endswith(".jira.json")
    assert not produced.is_binary  # jira is a text format: the JSON rides in ``body``
    issue = json.loads(produced.body)
    fields = issue["fields"]
    # A backlog deliverable becomes a Jira Epic, templated from the bound roles.
    assert issue["key"].startswith("PP-")  # key from the project name initials
    assert fields["summary"] == "Backlog: payments rollout"
    assert fields["issuetype"]["name"] == "Epic"
    assert fields["status"]["name"] == "Backlog"
    assert fields["project"]["id"] == "project:payments"


def test_issue_type_tracks_the_deliverable_kind() -> None:
    assert _issue(_event(kind="bug"))["fields"]["issuetype"]["name"] == "Bug"
    assert _issue(_event(kind="story"))["fields"]["issuetype"]["name"] == "Story"
    assert _issue(_event(kind="ticket"))["fields"]["issuetype"]["name"] == "Task"


def test_reporter_and_assignee_are_real_people() -> None:
    # No ``author`` role exists on a real playbook event; the reporter falls back to
    # an actor (the lead), and the assignee is a distinct teammate.
    fields = _issue()["fields"]
    assert fields["reporter"]["accountId"] == "person:ada"
    assert fields["assignee"]["accountId"] != "person:ada"


def test_reviewers_become_grounded_comments_with_in_window_stamps() -> None:
    comments = _issue()["fields"]["comment"]["comments"]
    assert [c["author"]["accountId"] for c in comments] == ["person:grace"]
    # First comment lands two hours after the issue is raised (in-window, D10).
    assert comments[0]["created"] == "2026-06-12T16:00:00+00:00"


def test_no_reviewers_is_a_valid_comment_less_issue() -> None:
    issue = _issue(_event(reviewers=[]))
    assert "comment" not in issue["fields"]


def test_produce_tags_jira_medium_mentions_with_valid_locators() -> None:
    produced = JiraProducer().produce(_event(), _world().projection(at=_T_EVENT), _client())
    entities = {m.entity_id for m in produced.mentions}
    assert {"person:ada", "person:grace", "project:payments"}.issubset(entities)
    for mention in produced.mentions:
        assert mention.locator.medium == "jira"
        span = produced.body[
            mention.locator.offset : mention.locator.offset + mention.locator.length
        ]
        assert span == mention.surface_form


def test_produce_builds_kg_node_and_edges() -> None:
    produced = JiraProducer().produce(_event(), _world().projection(at=_T_EVENT), _client())
    assert produced.node.type == "Artifact"
    assert produced.node.props["format"] == "jira"
    assert produced.node.props["path"] == produced.path
    by_type = {e.type for e in produced.edges}
    assert {"authored", "reviewed", "expresses"}.issubset(by_type)
    authored = [e for e in produced.edges if e.type == "authored"]
    assert authored[0].src == "person:ada"
    assert any(e.type == "expresses" and e.dst == "project:payments" for e in produced.edges)


def test_produce_is_deterministic() -> None:
    a = JiraProducer().produce(_event(), _world().projection(at=_T_EVENT), _client())
    b = JiraProducer().produce(_event(), _world().projection(at=_T_EVENT), _client())
    assert a.body == b.body
    assert a.node.props["issue_key"] == b.node.props["issue_key"]
    assert [m.to_dict() for m in a.mentions] == [m.to_dict() for m in b.mentions]


# -- the multi-modal fan-out (D6) -------------------------------------------


def test_backlog_kind_fans_out_to_markdown_and_jira() -> None:
    names = [p.name for p in _producers_for("backlog")]
    assert names == ["markdown", "jira"]
    # Non-fan-out kinds keep a single primary producer.
    assert _producer_for("backlog").name == "markdown"
    assert _producer_for("status_report").name == "word"
    assert _producer_for("kickoff_brief").name == "markdown"


def test_fan_out_yields_two_distinct_coexisting_artifacts() -> None:
    # One event rendered by both producers yields two artifacts with *distinct* ids
    # and paths that both survive ``apply_to_world`` and express the same subject —
    # the cross-modally KG-consistent corpus the fan-out is for.
    world = _world()
    view = world.projection(at=_T_EVENT)
    md = MarkdownProducer().produce(_event(), view, _client())
    jira = JiraProducer().produce(_event(), view, _client())

    assert md.artifact_id != jira.artifact_id
    assert md.path != jira.path
    apply_to_world(world, [md, jira])
    assert world.get_node(md.artifact_id) is not None
    assert world.get_node(jira.artifact_id) is not None
    # Both renderings carry the same source event and express the same subject.
    assert md.node.props["event"] == jira.node.props["event"]
    md_expresses = {e.dst for e in md.edges if e.type == "expresses"}
    jira_expresses = {e.dst for e in jira.edges if e.type == "expresses"}
    assert "project:payments" in md_expresses & jira_expresses
