"""Code-repository artifacts for coding-team worlds (``[code]``).

A coding agent's memory is about code, not only documents: which datastore the
service talks to, which API version it serves, which flag gates the launch, why
a migration happened. :func:`build_repositories` writes, for every project of an
engineering-kind department that has a fact ledger, a small repository-like
corpus under ``artifacts/<scenario>/repo/``:

* ``config/service.toml`` — the engineering facts as configuration, at their
  value true at the window end (``asserts`` stance ``current``);
* ``README.md`` — what the service is, who owns it, how it deploys (current);
* ``CHANGELOG.md`` — one dated entry per fact change (stance ``update``);
* ``src/storage.py`` — a client stub whose header comment still names the
  **first** datastore when the datastore has since changed (stance ``stale``:
  the comment nobody updated);
* ``commits.jsonl`` — one line per ``CommitPushed`` event (sha, author, date,
  message); a commit on a change day names the change.

Every file is an ``Artifact`` node with ``authored``/``expresses`` edges and its
``asserts`` edges labelled as above, so the gold says which file is right. All
of it is a pure function of the world; no model call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from enterprise_sim.core.events import EventJournal
from enterprise_sim.core.world import Edge, Node, World
from enterprise_sim.facts.ledger import FactRecord, Ledger
from enterprise_sim.producers.artifact import ProducedArtifact

__all__ = ["build_repositories"]

_ENGINEERING = frozenset({"engineering", "platform", "security"})
_CONFIG_KEYS = {
    "datastore": ("storage", "engine"),
    "api_version": ("api", "version"),
    "feature_flag": ("launch", "flag"),
    "deploy_cadence": ("deploy", "cadence"),
    "latency_target": ("slo", "p99_latency"),
}


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")


def _asserts(artifact_id: str, record: FactRecord, stance: str, number: int, at: datetime) -> Edge:
    version = record.versions[number - 1]
    return Edge(
        f"edge:asserts:{artifact_id}->{record.id}@v{number}",
        "asserts",
        artifact_id,
        record.id,
        at,
        props={
            "stance": stance,
            "value": version.value,
            "true_version": record.versions[-1].number,
            "stated_version": number,
            "cited": False,
        },
    )


def _artifact(
    *,
    artifact_id: str,
    path: str,
    kind: str,
    title: str,
    body: str,
    at: datetime,
    project: str,
    authors: list[str],
    asserts: list[Edge],
) -> ProducedArtifact:
    node = Node(
        artifact_id,
        "Artifact",
        at,
        props={
            "kind": kind,
            "medium": "code",
            "title": title,
            "path": path,
            "date": at.date().isoformat(),
            "authors": authors,
            "reviewers": [],
        },
        aliases=[],
    )
    edges = [
        Edge(f"edge:authored:{a}->{artifact_id}", "authored", a, artifact_id, at) for a in authors
    ]
    edges.append(
        Edge(f"edge:expresses:{artifact_id}->{project}", "expresses", artifact_id, project, at)
    )
    return ProducedArtifact(
        artifact_id=artifact_id,
        path=path,
        fmt="code",
        body=body,
        node=node,
        edges=[*edges, *asserts],
        metadata={"artifact_id": artifact_id, "path": path},
    )


def build_repositories(
    world: World, ledger: Ledger, journal: EventJournal, *, end: datetime
) -> list[ProducedArtifact]:
    """The repository artifacts of every engineering project in ``world``."""
    produced: list[ProducedArtifact] = []
    for project_id, fact_ids in sorted(ledger.by_project.items()):
        project = world.get_node(project_id)
        if project is None or str(project.props.get("department")) not in _ENGINEERING:
            continue
        records = {ledger.facts[f].attribute.name: ledger.facts[f] for f in fact_ids}
        if not set(records) & set(_CONFIG_KEYS):
            continue
        scenario = next(
            (e.dst for e in world.out_edges(project_id, "under")),
            project_id,
        )
        base = f"artifacts/{_slug(scenario)}/repo"
        name = str(project.props.get("name", project_id))
        members = [e.src for e in world.in_edges(project_id, "member_of")]
        lead = next(
            (
                e.src
                for e in world.in_edges(project_id, "member_of")
                if e.props.get("role") == "lead"
            ),
            members[0] if members else "",
        )
        authors = [lead] if lead else []
        prefix = f"artifact:repo:{project_id.split(':', 1)[-1]}"

        # config/service.toml: current values
        lines = [f"# Service configuration for {name}.", ""]
        config_asserts: list[Edge] = []
        sections: dict[str, list[str]] = {}
        for attr, (section, key) in _CONFIG_KEYS.items():
            record = records.get(attr)
            if record is None:
                continue
            current = record.versions[-1]
            sections.setdefault(section, []).append(f"{key} = {json.dumps(current.value)}")
            config_asserts.append(
                _asserts(f"{prefix}:config", record, "current", current.number, end)
            )
        for section, entries in sections.items():
            lines += [f"[{section}]", *entries, ""]
        produced.append(
            _artifact(
                artifact_id=f"{prefix}:config",
                path=f"{base}/config/service.toml",
                kind="service_config",
                title=f"{name} service configuration",
                body="\n".join(lines).rstrip("\n") + "\n",
                at=end,
                project=project_id,
                authors=authors,
                asserts=config_asserts,
            )
        )

        # README.md: owner + deploy cadence, current
        readme_asserts: list[Edge] = []
        readme = [f"# {name}", "", f"Service code for {name}.", ""]
        for attr, sentence in (
            ("owner", "Day-to-day owner: {value}."),
            ("deploy_cadence", "This service deploys {value}."),
            ("datastore", "Records live in {value}."),
        ):
            record = records.get(attr)
            if record is not None:
                current = record.versions[-1]
                readme.append(sentence.format(value=current.value))
                readme_asserts.append(
                    _asserts(f"{prefix}:readme", record, "current", current.number, end)
                )
        produced.append(
            _artifact(
                artifact_id=f"{prefix}:readme",
                path=f"{base}/README.md",
                kind="readme",
                title=f"{name} README",
                body="\n".join(readme) + "\n",
                at=end,
                project=project_id,
                authors=authors,
                asserts=readme_asserts,
            )
        )

        # CHANGELOG.md: every change, dated
        changes: list[tuple[datetime, str, FactRecord, int]] = []
        for record in records.values():
            for older, newer in zip(record.versions, record.versions[1:], strict=False):
                changes.append(
                    (
                        newer.valid_from,
                        f"- {newer.valid_from.date().isoformat()}: {record.attribute.label} "
                        f"changed from {older.value} to {newer.value}.",
                        record,
                        newer.number,
                    )
                )
        changes.sort(key=lambda c: (c[0], c[1]))
        if changes:
            changelog_id = f"{prefix}:changelog"
            produced.append(
                _artifact(
                    artifact_id=changelog_id,
                    path=f"{base}/CHANGELOG.md",
                    kind="changelog",
                    title=f"{name} changelog",
                    body="\n".join([f"# Changelog: {name}", "", *(c[1] for c in changes)]) + "\n",
                    at=end,
                    project=project_id,
                    authors=authors,
                    asserts=[
                        _asserts(changelog_id, rec, "update", num, at)
                        for at, _, rec, num in changes
                    ],
                )
            )

        # src/storage.py: a stale header comment when the datastore changed
        store = records.get("datastore")
        if store is not None:
            first, current = store.versions[0], store.versions[-1]
            stance, number = (
                ("stale", 1) if len(store.versions) > 1 else ("current", current.number)
            )
            storage_id = f"{prefix}:storage"
            code = [
                f'"""Storage client for {name}.',
                "",
                f"Talks to {first.value}; see config/service.toml for connection settings.",
                '"""',
                "",
                "",
                "def connect(settings):",
                '    """Open a connection using the [storage] section of the service config."""',
                '    return settings["storage"]',
            ]
            produced.append(
                _artifact(
                    artifact_id=storage_id,
                    path=f"{base}/src/storage.py",
                    kind="source_file",
                    title=f"{name} storage client",
                    body="\n".join(code) + "\n",
                    at=first.valid_from,
                    project=project_id,
                    authors=authors,
                    asserts=[_asserts(storage_id, store, stance, number, first.valid_from)],
                )
            )

        # commits.jsonl: one line per CommitPushed event of the project
        change_days = {c[0].date(): (c[2], c[3]) for c in changes}
        commits: list[str] = []
        for event in journal.ordered():
            if event.type != "CommitPushed" or event.project != project_id:
                continue
            sha = hashlib.sha1(event.id.encode()).hexdigest()[:10]
            author_ids = [p for ids in event.actors.values() for p in ids][:1]
            node = world.get_node(author_ids[0]) if author_ids else None
            author = str(node.props.get("name")) if node is not None else "unknown"
            changed = change_days.get(event.timestamp.date())
            if changed is not None:
                rec, num = changed
                message = f"{rec.attribute.label}: switch to {rec.versions[num - 1].value}"
            else:
                message = f"work on {name.lower()}"
            commits.append(
                json.dumps(
                    {
                        "sha": sha,
                        "author": author,
                        "date": event.timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
                        "message": message,
                    },
                    sort_keys=True,
                )
            )
        if commits:
            produced.append(
                _artifact(
                    artifact_id=f"{prefix}:commits",
                    path=f"{base}/commits.jsonl",
                    kind="commit_log",
                    title=f"{name} commit log",
                    body="\n".join(commits) + "\n",
                    at=end,
                    project=project_id,
                    authors=authors,
                    asserts=[],
                )
            )
    return produced
