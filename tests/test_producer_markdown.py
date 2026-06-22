"""Tests for the markdown producer (ARCHITECTURE.md §16, §11.3).

Acceptance (esim-fc7a20d0): renders a grounded deliverable; mentions are
recorded; reference edges are created. Also covers templated grounding, the
single repair pass + validation issue, determinism, and applying the result to a
World + serializing the §11.4 side files.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from enterprise_sim.core.events import Deliverable, Event
from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.llm.backends import estimate_tokens
from enterprise_sim.core.llm.prompt import Prompt
from enterprise_sim.core.llm.types import Completion, TokenUsage
from enterprise_sim.core.world import Node, World
from enterprise_sim.producers import (
    MarkdownProducer,
    ProducerContext,
    apply_to_world,
    mention_records,
    provenance_records,
)

_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_T_EVENT = datetime(2026, 6, 12, 14, 0, tzinfo=UTC)


def _person(node_id: str, name: str, *, aliases: list[str] | None = None) -> Node:
    return Node(node_id, "Person", _T0, props={"name": name}, aliases=aliases or [])


def _artifact(node_id: str, title: str, *, at: datetime = _T0) -> Node:
    return Node(node_id, "Artifact", at, props={"title": title, "kind": "status_report"})


def _world() -> World:
    world = World()
    world.add_node(_person("person:ada", "Ada Lovelace", aliases=["Ada"]))
    world.add_node(_person("person:alan", "Alan Turing"))
    world.add_node(Node("project:payments", "Project", _T0, props={"name": "Payments Platform"}))
    # Two prior artifacts the model may cite (the candidate reference set).
    world.add_node(_artifact("artifact:status-w11", "Status Week 11"))
    world.add_node(_artifact("artifact:design-pay", "Payments Design"))
    return world


def _event() -> Event:
    return Event(
        id="evt:status-w12",
        type="DeliverableDrafted",
        timestamp=_T_EVENT,
        actors={"author": ["person:ada"], "reviewers": ["person:alan"]},
        initiative="init:payments",
        project="project:payments",
        subjects=["project:payments"],
        deliverable=Deliverable(kind="status_report", medium="document"),
        payload={"topic": "payments rollout", "tone": "neutral", "title": "Weekly payments update"},
    )


def _fake_client() -> LLMClient:
    from enterprise_sim.core.llm import LLMConfig

    return LLMClient.from_config(LLMConfig(backend="fake", cache_enabled=False))


# -- end-to-end with the fake backend ---------------------------------------


def test_produce_renders_grounded_markdown_artifact() -> None:
    produced = MarkdownProducer().produce(_event(), _world(), _fake_client())
    assert produced.fmt == "markdown"
    assert produced.path == "artifacts/artifact-status-w12.md"
    # Templated header is grounded by construction (§16.2.2): real author/reviewer.
    assert "# Weekly payments update" in produced.body
    assert "**Author:** Ada Lovelace" in produced.body
    assert "**Reviewers:** Alan Turing" in produced.body
    # The Artifact node carries kind/medium/path/authorship.
    assert produced.node.type == "Artifact"
    assert produced.node.props["authors"] == ["person:ada"]
    assert produced.node.props["path"] == produced.path
    # A grounded draft needs no repair, so no validation issues are logged.
    assert produced.issues == []


def test_produce_records_mentions() -> None:
    produced = MarkdownProducer().produce(_event(), _world(), _fake_client())
    entities = {m.entity_id for m in produced.mentions}
    # The author/reviewer names appear in the templated header → tagged as mentions.
    assert "person:ada" in entities
    assert "person:alan" in entities
    for mention in produced.mentions:
        span = produced.body[
            mention.locator.offset : mention.locator.offset + mention.locator.length
        ]
        assert span == mention.surface_form


def test_produce_creates_reference_edges() -> None:
    produced = MarkdownProducer().produce(_event(), _world(), _fake_client())
    ref_edges = [e for e in produced.edges if e.type == "references"]
    assert ref_edges, "fake backend cites a deterministic subset of candidates"
    for edge in ref_edges:
        assert edge.src == produced.artifact_id
        assert edge.dst in {"artifact:status-w11", "artifact:design-pay"}
    # References are also templated into the body and the json metadata twin.
    assert "## References" in produced.body
    assert produced.metadata["references"] == [e.dst for e in ref_edges]


def test_produce_creates_authored_reviewed_and_provenance_edges() -> None:
    produced = MarkdownProducer().produce(_event(), _world(), _fake_client())
    by_type = {e.type: e for e in produced.edges}
    assert by_type["authored"].src == "person:ada"
    assert by_type["authored"].dst == produced.artifact_id
    assert by_type["reviewed"].src == "person:alan"
    # Provenance: the artifact expresses the project it is about (in-scope subject).
    expresses = [e for e in produced.edges if e.type == "expresses"]
    assert any(e.dst == "project:payments" for e in expresses)


def test_produce_is_deterministic() -> None:
    a = MarkdownProducer().produce(_event(), _world(), _fake_client())
    b = MarkdownProducer().produce(_event(), _world(), _fake_client())
    assert a.body == b.body
    assert [e.id for e in a.edges] == [e.id for e in b.edges]
    assert [m.to_dict() for m in a.mentions] == [m.to_dict() for m in b.mentions]


def test_produce_uses_existing_abstract_artifact_subject() -> None:
    event = _event()
    event.subjects = ["artifact:weekly-status", "project:payments"]
    produced = MarkdownProducer().produce(event, _world(), _fake_client())
    assert produced.artifact_id == "artifact:weekly-status"


def test_produce_with_no_candidates_creates_no_reference_edges() -> None:
    world = World()
    world.add_node(_person("person:ada", "Ada Lovelace"))
    world.add_node(_person("person:alan", "Alan Turing"))
    world.add_node(Node("project:payments", "Project", _T0, props={"name": "Payments Platform"}))
    produced = MarkdownProducer().produce(_event(), world, _fake_client())
    assert [e for e in produced.edges if e.type == "references"] == []
    assert "## References" not in produced.body


# -- detect + single repair (§16.2.3 / D17) ---------------------------------


class _ScriptedBackend:
    """A backend whose ``generate_content`` returns queued bodies in order.

    Lets a test drive the producer's detect-and-repair loop deterministically: the
    first body names an out-of-scope person; later bodies are clean (or not).
    """

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
        usage = TokenUsage(output_tokens=estimate_tokens(body))
        return Completion(text=body, usage=usage, model=model)


def _client_with(backend: _ScriptedBackend) -> LLMClient:
    from enterprise_sim.core.llm import LLMConfig

    return LLMClient(backend, config=LLMConfig(backend="scripted", cache_enabled=False))


def test_repair_pass_fixes_unresolved_name() -> None:
    backend = _ScriptedBackend(
        [
            "Ada Lovelace met Grace Hopper to plan the rollout.",  # hallucinated name
            "Ada Lovelace planned the rollout with the team.",  # clean rewrite
        ]
    )
    produced = MarkdownProducer().produce(_event(), _world(), _client_with(backend))
    assert backend.calls == 2  # one draft + one repair
    assert produced.issues == []
    assert "Grace Hopper" not in produced.body


def test_unrepaired_name_becomes_validation_issue() -> None:
    backend = _ScriptedBackend(["Ada Lovelace and Grace Hopper shipped it."])  # always dirty
    produced = MarkdownProducer().produce(_event(), _world(), _client_with(backend))
    assert backend.calls == 2  # draft + the single repair, then give up
    assert len(produced.issues) == 1
    issue = produced.issues[0]
    assert issue.kind == "unresolved_mention"
    assert "Grace Hopper" in issue.details["names"]
    # The artifact is kept despite the issue (§16.2.3 / D17).
    assert "Ada Lovelace" in produced.body


def test_clean_draft_skips_repair() -> None:
    backend = _ScriptedBackend(["Ada Lovelace led the payments rollout review."])
    MarkdownProducer().produce(_event(), _world(), _client_with(backend))
    assert backend.calls == 1  # no repair needed


def test_context_feeds_prompt_and_directory() -> None:
    backend = _ScriptedBackend(["Ada Lovelace led the rollout."])
    ctx = ProducerContext(
        company_profile="ACME Corp — a payments company.",
        scenario_context="Scenario: payments platform rollout.",
        artifacts_dir="corpus/docs",
    )
    produced = MarkdownProducer().produce(_event(), _world(), _client_with(backend), ctx)
    assert produced.path.startswith("corpus/docs/")
    prompt_text = backend.prompts[0].text
    # Stable context blocks (cacheable prefix) and the constrained roster are present.
    assert "ACME Corp" in prompt_text
    assert "Scenario: payments platform rollout." in prompt_text
    assert "refer ONLY to the following" in prompt_text
    assert "Ada Lovelace" in prompt_text


# -- applying to the KG + side files ----------------------------------------


def test_apply_to_world_and_side_files() -> None:
    world = _world()
    produced = MarkdownProducer().produce(_event(), world, _fake_client())
    apply_to_world(world, [produced])
    # The Artifact node and its edges are now in the graph.
    assert world.get_node(produced.artifact_id) is not None
    for edge in produced.edges:
        assert world.get_edge(edge.id) is not None
    # Applying twice is idempotent (no duplicate-id error).
    apply_to_world(world, [produced])

    prov = provenance_records([produced])
    # Provenance is keyed by target; the artifact's own id is one target.
    assert any(r["target_id"] == produced.artifact_id for r in prov)
    assert all(produced.path in {a["path"] for a in r["artifacts"]} for r in prov)

    mentions = mention_records([produced])
    assert mentions == [m.to_dict() for m in produced.mentions]
