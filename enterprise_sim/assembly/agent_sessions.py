"""Coding-agent session transcripts for engineering projects (``[code] sessions_per_project``).

Spindle captures memory from coding sessions, so a coding world needs sessions, not
only documents: what an agent was asked, which files it read and what they said *on
that day*, what it changed, and where it caught a stale comment. :func:`build_sessions`
writes, for every engineering project with a fact ledger (the projects that get a
repository from :mod:`enterprise_sim.assembly.code_repo`), up to
``sessions_per_project`` transcripts on the project's commit days, under
``artifacts/<scenario>/sessions/``, one JSON object per turn:

* ``{"turn", "role": "user", "type": "message", "text"}``: the task;
* ``{"turn", "role": "assistant", "type": "tool_use", "tool", "input"}``: a read, an
  edit or a test run;
* ``{"turn", "role": "tool", "type": "tool_result", "tool", "text"}``: what the tool
  returned. A read of ``config/service.toml`` shows the values true at the session's
  time, not at the window end;
* ``{"turn", "role": "assistant", "type": "message", "text"}``: the agent's notes.

Each session is consistent with the fact ledger and labelled in the gold KG through
``asserts`` edges:

* every value the session reads from the config is the version true then
  (``current``);
* on a change day the session makes the change (``update``: from the old to the new
  value);
* after the datastore changed, reading ``src/storage.py`` shows its stale header
  comment (``stale``), and the agent writes down the correction (``current``): the
  comment names the old store, the config the new one. ``corrected`` on the session
  node lists such facts.

A pure function of the world: no model call, deterministic, the same in every
backend. The session's author is the commit's author.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from enterprise_sim.assembly.code_repo import _CONFIG_KEYS, _slug
from enterprise_sim.core.events import EventJournal
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.facts.ledger import FactRecord, Ledger
from enterprise_sim.producers.artifact import ProducedArtifact

__all__ = ["build_sessions"]

_ENGINEERING = frozenset({"engineering", "platform", "security"})
_CHORES = (
    "Fix the flaky integration test in the {area} suite.",
    "Add structured logging around the {area} request path.",
    "Tidy up the {area} module and remove dead code.",
    "Investigate the slow {area} endpoint reported in the last review.",
    "Add a regression test for the {area} bug fixed last week.",
)
_AREAS = ("storage", "api", "deploy", "cache", "auth", "billing")


def _assertion(
    session_id: str, record: FactRecord, stance: str, number: int, at: datetime, n: int
) -> Edge:
    version = record.versions[number - 1]
    return Edge(
        f"edge:asserts:{session_id}->{record.id}@v{number}#{n}",
        "asserts",
        session_id,
        record.id,
        at,
        props={
            "stance": stance,
            "value": version.value,
            "true_version": record.version_at(at).number,
            "stated_version": number,
            "cited": False,
        },
    )


def _config_text(name: str, records: dict[str, FactRecord], at: datetime) -> str:
    lines = [f"# Service configuration for {name}.", ""]
    sections: dict[str, list[str]] = {}
    for attr, (section, key) in _CONFIG_KEYS.items():
        record = records.get(attr)
        if record is not None:
            value = record.version_at(at).value
            sections.setdefault(section, []).append(f"{key} = {json.dumps(value)}")
    for section, entries in sections.items():
        lines += [f"[{section}]", *entries, ""]
    return "\n".join(lines).rstrip("\n")


def _pick(events: list[Any], count: int) -> list[Any]:
    """``count`` events spread evenly over ``events`` (all of them when fewer)."""
    if len(events) <= count:
        return events
    step = len(events) / count
    return [events[int(i * step)] for i in range(count)]


def build_sessions(
    world: World, ledger: Ledger, journal: EventJournal, *, per_project: int
) -> list[ProducedArtifact]:
    """Up to ``per_project`` agent session transcripts for every engineering project."""
    produced: list[ProducedArtifact] = []
    if per_project <= 0:
        return produced
    for project_id, fact_ids in sorted(ledger.by_project.items()):
        project = world.get_node(project_id)
        if project is None or str(project.props.get("department")) not in _ENGINEERING:
            continue
        records = {ledger.facts[f].attribute.name: ledger.facts[f] for f in fact_ids}
        if not set(records) & set(_CONFIG_KEYS):
            continue
        name = str(project.props.get("name", project_id))
        scenario = next((e.dst for e in world.out_edges(project_id, "under")), project_id)
        base = f"artifacts/{_slug(scenario)}/sessions"
        commits = [
            e for e in journal.ordered() if e.type == "CommitPushed" and e.project == project_id
        ]
        changes: dict[Any, tuple[FactRecord, int]] = {}
        for record in records.values():
            if record.attribute.name not in _CONFIG_KEYS:
                continue
            for version in record.versions[1:]:
                changes.setdefault(version.valid_from.date(), (record, version.number))
        # the first commit of each change day makes the change; other commits are chores
        first_of_day: dict[Any, Any] = {}
        for event in commits:
            first_of_day.setdefault(event.timestamp.date(), event)
        on_change = [e for d, e in sorted(first_of_day.items()) if d in changes]
        makers = {e.id for e in on_change}
        others = [e for e in commits if e.id not in makers]
        chosen = on_change[:per_project]
        chosen += _pick(others, per_project - len(chosen))
        for event in sorted(chosen, key=lambda e: (e.timestamp, e.id)):
            change = changes.get(event.timestamp.date()) if event.id in makers else None
            produced.append(_session(world, event, project_id, name, base, records, change))
    return produced


def _session(
    world: World,
    event: Any,
    project_id: str,
    name: str,
    base: str,
    records: dict[str, FactRecord],
    changed: tuple[FactRecord, int] | None,
) -> ProducedArtifact:
    """One session. A change session reads the repository just before the change takes
    effect (so it sees the old value), then makes it."""
    from datetime import timedelta

    at: datetime = event.timestamp
    if changed is not None:
        at = changed[0].versions[changed[1] - 1].valid_from - timedelta(minutes=1)
    digest = hashlib.sha256(event.id.encode()).hexdigest()
    session_id = f"artifact:session:{project_id.split(':', 1)[-1]}:{digest[:10]}"
    author_ids = [p for ids in event.actors.values() for p in ids][:1]
    turns: list[dict[str, Any]] = []
    asserts: list[Edge] = []
    corrected: list[str] = []

    def say(role: str, kind: str, **fields: Any) -> None:
        turns.append({"turn": len(turns) + 1, "role": role, "type": kind, **fields})

    if changed is not None:
        target, number = changed
        old, new = target.versions[number - 2].value, target.versions[number - 1].value
        task = f"Switch the {target.attribute.label} of {name} from {old} to {new}."
    else:
        pick = int(digest[10:18], 16)
        area = _AREAS[pick % len(_AREAS)]
        task = _CHORES[(pick // len(_AREAS)) % len(_CHORES)].format(area=area)
    say("user", "message", text=task)
    say("assistant", "message", text="I'll start by reading the service configuration.")
    say("assistant", "tool_use", tool="read_file", input={"path": "config/service.toml"})
    say("tool", "tool_result", tool="read_file", text=_config_text(name, records, at))
    for attr in _CONFIG_KEYS:
        read = records.get(attr)
        if read is not None:
            version = read.version_at(at)
            asserts.append(_assertion(session_id, read, "current", version.number, at, 1))

    store = records.get("datastore")
    if store is not None and len(store.versions) > 1 and store.version_at(at).number > 1:
        first, now = store.versions[0].value, store.version_at(at).value
        say("assistant", "tool_use", tool="read_file", input={"path": "src/storage.py"})
        say(
            "tool",
            "tool_result",
            tool="read_file",
            text=f'"""Storage client for {name}.\n\nTalks to {first}; see config/service.toml '
            'for connection settings.\n"""',
        )
        say(
            "assistant",
            "message",
            text=f"The header comment in src/storage.py still says {first}, but "
            f"config/service.toml says {now}. The comment is stale; I am going by the config.",
        )
        asserts.append(_assertion(session_id, store, "stale", 1, at, 2))
        asserts.append(_assertion(session_id, store, "current", store.version_at(at).number, at, 3))
        corrected.append(store.id)

    if changed is not None:
        target, number = changed
        section, key = _CONFIG_KEYS[target.attribute.name]
        old, new = target.versions[number - 2].value, target.versions[number - 1].value
        say(
            "assistant",
            "tool_use",
            tool="edit_file",
            input={
                "path": "config/service.toml",
                "old": f"{key} = {json.dumps(old)}",
                "new": f"{key} = {json.dumps(new)}",
            },
        )
        say("tool", "tool_result", tool="edit_file", text=f"[{section}] {key} updated.")
        say("assistant", "tool_use", tool="run_command", input={"command": "make test"})
        say("tool", "tool_result", tool="run_command", text="All tests passed.")
        say(
            "assistant",
            "message",
            text=f"Done: the {target.attribute.label} of {name} is now {new} (was {old}).",
        )
        when = target.versions[number - 1].valid_from
        asserts.append(_assertion(session_id, target, "update", number, when, 4))
    else:
        say("assistant", "tool_use", tool="run_command", input={"command": "make test"})
        say("tool", "tool_result", tool="run_command", text="All tests passed.")
        say("assistant", "message", text="Done. The change is small and the tests pass.")

    path = f"{base}/{at.strftime('%Y-%m-%dt%H-%M-%S')}-{digest[:8]}.session.jsonl"
    node = Node(
        session_id,
        "Artifact",
        at,
        props={
            "kind": "agent_session",
            "medium": "session",
            "title": f"Agent session: {task}",
            "path": path,
            "date": at.date().isoformat(),
            "authors": author_ids,
            "reviewers": [],
            "event": event.id,
            "corrected": corrected,
        },
        aliases=[],
    )
    edges = [
        Edge(f"edge:authored:{a}->{session_id}", "authored", a, session_id, at) for a in author_ids
    ]
    edges.append(
        Edge(f"edge:expresses:{session_id}->{project_id}", "expresses", session_id, project_id, at)
    )
    body = "\n".join(json.dumps(t, sort_keys=True, ensure_ascii=False) for t in turns) + "\n"
    return ProducedArtifact(
        artifact_id=session_id,
        path=path,
        fmt="session",
        body=body,
        node=node,
        edges=[*edges, *asserts],
        metadata={"artifact_id": session_id, "path": path},
    )
