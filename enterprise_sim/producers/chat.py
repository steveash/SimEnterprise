"""The ``chat`` producer: a team-channel thread beside a document (``[chat]``).

Most stale facts in real teams live in chat: short, informal messages, often
restating what a document said, a few hours or days later. For a deliverable
event the corpus picks (``[chat] rate``), :class:`ChatProducer` writes one
thread to ``<slug>.chat.jsonl``:

* a root message by the document's author announcing it (one model call,
  grounded like every producer's prose and noised by ``[messiness]``);
* one message per ledger fact the event states, **one fact per message** and
  each on one line (so a test set can declare the value from a single field),
  posted by the author or a teammate, minutes to hours after the root;
* the thread's channel is the department's (``#<department>``).

Each line is a JSON object ``{"ts", "channel", "user", "thread", "text"}``;
users are display names, never addresses. The artifact id ends in ``:chat`` so
the thread is its own ``Artifact`` node beside the document, and the corpus
writes its ``asserts`` edges from the same event facts.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from datetime import timedelta

from enterprise_sim.core.events import Event
from enterprise_sim.core.llm import LLMClient, assemble_prompt
from enterprise_sim.core.world import Edge, Node, WorldView
from enterprise_sim.producers.artifact import ProducedArtifact
from enterprise_sim.producers.grounding import Roster, author_roles, tag_mentions
from enterprise_sim.producers.markdown import ProducerContext

__all__ = ["ChatProducer", "chat_artifact_id"]

_AUTHOR_ROLES = ("author", "authors")
_LEADS = ("FYI:", "Heads up:", "Reminder:", "Quick note:", "")


def chat_artifact_id(event: Event) -> str:
    """The thread's artifact id: the event's document id plus ``:chat``."""
    return f"artifact:{event.id.split(':', 1)[-1]}:chat"


def _names(view: WorldView, ids: Sequence[str]) -> list[str]:
    out: list[str] = []
    for pid in ids:
        node = view.get_node(pid)
        if node is not None and node.props.get("name") and str(node.props["name"]) not in out:
            out.append(str(node.props["name"]))
    return out


class ChatProducer:
    """Render an event's channel thread (see the module docstring)."""

    name = "chat"
    formats: Sequence[str] = ("chat",)
    handles: Sequence[str] = ("chat_thread",)

    def produce(
        self,
        event: Event,
        view: WorldView,
        client: LLMClient,
        ctx: ProducerContext | None = None,
    ) -> ProducedArtifact:
        ctx = ctx or ProducerContext()
        roster = Roster.from_worldview(view)
        artifact_id = chat_artifact_id(event)
        slug = "".join(ch if ch.isalnum() else "-" for ch in artifact_id.lower()).strip("-")
        path = f"{ctx.artifacts_dir}/{slug}.chat.jsonl"
        author_ids: list[str] = []
        for role in author_roles(event, _AUTHOR_ROLES):
            author_ids += [p for p in event.actors.get(role, []) if p not in author_ids]
        everyone = [p for ids in event.actors.values() for p in ids]
        authors = _names(view, author_ids) or _names(view, everyone)[:1] or ["Someone"]
        team = _names(view, everyone) or authors
        kind = event.deliverable.kind if event.deliverable else "update"
        department = ""
        for pid in author_ids:
            node = view.get_node(pid)
            if node is not None:
                department = str(node.props.get("department", ""))
                break
        channel = f"#{(department or 'team').replace('_', '-')}"
        topic = str(event.payload.get("topic") or event.payload.get("intent") or kind)

        brief = "\n".join(
            [
                f"Task: write the body of a chat message in {channel}.",
                f"Title: {kind.replace('_', ' ')} posted.",
                f"Date: {event.timestamp.date().isoformat()}.",
                f"Topic: {topic}.",
                f"Authored by: {authors[0]}.",
                "",
                roster.roster_block(),
            ]
        )
        system = (
            "You write one short, informal team-chat message (one or two sentences) "
            "announcing a document to colleagues. Refer only to roster names."
        )
        result = client.generate_content(assemble_prompt(system=system, brief=brief))
        root_text = " ".join(result.content.split())
        root_text = ctx.messy(root_text, key=f"{artifact_id}#root", roster=roster)

        digest = hashlib.sha256(artifact_id.encode()).hexdigest()
        rng = random.Random(int(digest[:16], 16))
        at = event.timestamp + timedelta(minutes=rng.randint(5, 90))
        thread = at.strftime("%Y-%m-%dT%H:%M:%S")
        lines = [
            {
                "ts": thread,
                "channel": channel,
                "user": authors[0],
                "thread": thread,
                "text": root_text,
            }
        ]
        for index, fact in enumerate(event.payload.get("facts") or []):
            at += timedelta(minutes=rng.randint(3, 240))
            lead = rng.choice(_LEADS)
            text = " ".join(f"{lead} {fact['statement']}".split())
            text = ctx.messy(text, key=f"{artifact_id}#m{index}", roster=roster)
            lines.append(
                {
                    "ts": at.strftime("%Y-%m-%dT%H:%M:%S"),
                    "channel": channel,
                    "user": rng.choice(team),
                    "thread": thread,
                    "text": text,
                }
            )
        body = "".join(
            json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines
        )
        node = Node(
            id=artifact_id,
            type="Artifact",
            created_at=event.timestamp,
            props={
                "kind": "chat_thread",
                "medium": "chat",
                "title": f"{channel} thread: {kind.replace('_', ' ')}",
                "path": path,
                "date": event.timestamp.date().isoformat(),
                "authors": author_ids,
                "reviewers": [],
                "event": event.id,
                "channel": channel,
            },
            aliases=[],
        )
        edges = [
            Edge(
                f"edge:authored:{pid}->{artifact_id}", "authored", pid, artifact_id, event.timestamp
            )
            for pid in author_ids
        ]
        for subject in event.subjects:
            if view.get_node(subject) is not None and subject != artifact_id:
                edges.append(
                    Edge(
                        f"edge:expresses:{artifact_id}->{subject}",
                        "expresses",
                        artifact_id,
                        subject,
                        event.timestamp,
                    )
                )
        mentions = tag_mentions(body, roster, artifact_path=path, medium="chat")
        return ProducedArtifact(
            artifact_id=artifact_id,
            path=path,
            fmt="chat",
            body=body,
            node=node,
            edges=edges,
            mentions=mentions,
            metadata={"artifact_id": artifact_id, "path": path, "channel": channel},
            cache_hit=result.cache_hit,
        )
