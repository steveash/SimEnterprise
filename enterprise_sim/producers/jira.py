"""The ``jira`` producer — events → grounded Jira-style issues (ARCHITECTURE.md §4, §16, D6/D12).

A new-modality producer (after ``markdown``/``outlook``/``word``, §16): it satisfies
the same :class:`~enterprise_sim.core.registry.plugins.Producer` protocol and is a
pure function of ``(event, view)``. It renders the *same* format-agnostic
:class:`~enterprise_sim.core.events.Event` into a **Jira-style issue** — a single
JSON object shaped like the Jira REST ``issue`` resource (``key`` + ``fields`` with
``summary``/``description``/``issuetype``/``status``/``priority``/``reporter``/
``assignee``/``comment``/``issuelinks``). The on-disk artifact is that JSON, which
also concatenates cleanly into an ``ndjson`` Jira export (one issue per line) when a
bulk view is wanted.

It is the producer that proves the registry's **multi-modal fan-out** (D6): the
binding map can route one deliverable kind to ``markdown`` *and* ``jira`` at once,
so a single ``backlog`` event yields both a narrative markdown twin and a Jira
issue — two cross-modally KG-consistent artifacts that ``expresses`` the same
subjects and carry the same ``event`` id. To keep both renderings as distinct KG
nodes (rather than colliding on one ``Artifact`` id), the jira artifact takes a
modality-discriminated id (``…:jira``) and path (``….jira.json``).

All three grounding layers carry over from the markdown producer (D30): a
:class:`~enterprise_sim.producers.grounding.Roster` constrains what the model may
name; the reporter/assignee/dates/links are *templated* from the event's bound
roles and verified references, never generated; and a single detect-and-repair
pass turns any surviving out-of-scope name in the description into a soft
validation issue while keeping the artifact (§16.2.3 / D17). Deterministic given a
deterministic backend (D10): the issue key is a stable function of the event id and
the comment timestamps are in-window offsets off the event instant.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from enterprise_sim.core.events import Event
from enterprise_sim.core.llm import LLMClient, Prompt, assemble_prompt
from enterprise_sim.core.registry import PRODUCERS
from enterprise_sim.core.world import Edge, Node, WorldView
from enterprise_sim.producers.artifact import Mention, ProducedArtifact, ValidationIssue
from enterprise_sim.producers.grounding import Roster, detect_unresolved_names, tag_mentions
from enterprise_sim.producers.markdown import ProducerContext

__all__ = ["JiraProducer"]

# KG node/edge type constants this producer reads and writes (§3) — shared with the
# other producers so the corpus stays cross-modally KG-consistent.
_N_ARTIFACT = "Artifact"
_N_PROJECT = "Project"
_E_AUTHORED = "authored"
_E_REVIEWED = "reviewed"
_E_EXPRESSES = "expresses"
_E_REFERENCES = "references"

# Roles in ``event.actors`` that map to the reporter (issue creator) and the
# commenters (reviewers). The reporter falls back to *any* actor (see
# :func:`_reporter`) so an issue raised by a ``by="engineers"`` step still names a
# real person even when no ``author`` role exists.
_REPORTER_ROLES = ("author", "authors", "reporter", "owner", "lead", "creator")
_COMMENTER_ROLES = ("reviewer", "reviewers", "commenter", "commenters")
_ASSIGNEE_ROLES = ("assignee", "owner", "engineer", "engineers", "developer", "lead")

# Abstract deliverable kind → Jira issue type. Anything unmapped is a ``Task``.
_ISSUE_TYPES = {
    "backlog": "Epic",
    "epic": "Epic",
    "story": "Story",
    "user_story": "Story",
    "bug": "Bug",
    "defect": "Bug",
    "incident": "Incident",
    "subtask": "Sub-task",
    "sub_task": "Sub-task",
    "task": "Task",
    "ticket": "Task",
    "issue": "Task",
}

# How long after the issue is raised the first comment lands, and the cadence
# between successive comments — small in-window offsets off the event instant so
# timestamps are deterministic and sit inside the issue's window.
_FIRST_COMMENT_DELAY = timedelta(hours=2)
_COMMENT_SPACING = timedelta(hours=1)

#: Fallback project key token when the event names no resolvable project.
_DEFAULT_PROJECT_KEY = "ESIM"


class JiraProducer:
    """Render an :class:`Event`'s deliverable to a grounded Jira-style issue.

    Satisfies the :class:`~enterprise_sim.core.registry.plugins.Producer` protocol
    (``name`` / ``formats`` / ``handles``). It is *not* a catch-all: it ``handles``
    the issue-shaped deliverable kinds, and the binding map may additionally fan a
    document kind (e.g. ``backlog``) out to it alongside ``markdown`` (D6).
    """

    name = "jira"
    formats: Sequence[str] = ("jira", "json", "ndjson")
    handles: Sequence[str] = (
        "backlog",
        "bug",
        "task",
        "story",
        "epic",
        "ticket",
        "issue",
        "subtask",
        "incident",
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
        The rendered JSON rides in :attr:`ProducedArtifact.body` (it is a text
        format), and is the same string the mention tagger runs over.
        """
        ctx = ctx or ProducerContext()
        roster = Roster.from_worldview(view)

        kind = event.deliverable.kind if event.deliverable else "task"
        medium = event.deliverable.medium if event.deliverable else "jira"
        issue_type = _issue_type_for(kind)
        title = _title_for(event, kind)
        artifact_id = _artifact_id_for(event)
        path = f"{ctx.artifacts_dir}/{_slug(artifact_id)}.jira.json"
        issue_key = _issue_key(event, view)

        reporter = _reporter(event, view)
        assignee = _assignee(event, view, reporter)
        commenters = _resolve_people(event, view, _COMMENTER_ROLES)

        description, references, issues = self._generate_grounded(
            client=client,
            event=event,
            kind=kind,
            issue_type=issue_type,
            title=title,
            roster=roster,
            reporter=reporter,
            ctx=ctx,
            path=path,
        )
        comments = self._build_comments(
            commenters=commenters,
            client=client,
            ctx=ctx,
            roster=roster,
            issue_type=issue_type,
            title=title,
            event=event,
        )

        body = _render_issue_json(
            issue_key=issue_key,
            issue_type=issue_type,
            kind=kind,
            title=title,
            description=description,
            event=event,
            reporter=reporter,
            assignee=assignee,
            references=references,
            comments=comments,
            view=view,
        )
        mentions = tag_mentions(body, roster, artifact_path=path, medium="jira")

        node = _artifact_node(
            artifact_id=artifact_id,
            kind=kind,
            medium=medium,
            issue_type=issue_type,
            issue_key=issue_key,
            title=title,
            event=event,
            reporter=reporter,
            commenters=commenters,
            path=path,
        )
        edges = _build_edges(
            artifact_id=artifact_id,
            event=event,
            view=view,
            reporter=reporter,
            commenters=commenters,
            references=references,
        )
        metadata = _metadata(
            artifact_id=artifact_id,
            kind=kind,
            medium=medium,
            issue_type=issue_type,
            issue_key=issue_key,
            title=title,
            path=path,
            event=event,
            reporter=reporter,
            assignee=assignee,
            commenters=commenters,
            references=references,
            comments=comments,
            mentions=mentions,
            issues=issues,
        )
        return ProducedArtifact(
            artifact_id=artifact_id,
            path=path,
            fmt="jira",
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
        issue_type: str,
        title: str,
        roster: Roster,
        reporter: Node | None,
        ctx: ProducerContext,
        path: str,
    ) -> tuple[str, tuple[str, ...], list[ValidationIssue]]:
        """Generate the issue description, then run the detect + single-repair loop (D30.3).

        Mirrors the markdown producer's single-repair loop: a draft that names an
        out-of-scope entity is regenerated once with a corrective instruction;
        whatever is still unresolved becomes a soft validation issue and the
        artifact is kept (§16.2.3 / D17).
        """
        system = _system_prompt(issue_type)
        stable = _stable_context(ctx)
        brief = _task_brief(
            event=event,
            kind=kind,
            issue_type=issue_type,
            title=title,
            roster=roster,
            reporter=reporter,
            candidates=tuple(event.subjects),
        )
        prompt = assemble_prompt(system=system, stable_context=stable, brief=brief)
        result = client.generate_content(prompt, candidate_references=list(event.subjects))
        description = result.content
        references = result.references_used

        unresolved = detect_unresolved_names(description, roster)
        if unresolved:
            repaired = client.generate_content(
                _repair_prompt(system, stable, brief, unresolved),
                candidate_references=list(event.subjects),
            )
            description = repaired.content
            references = repaired.references_used
            unresolved = detect_unresolved_names(description, roster)

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
        return description, references, issues

    # -- issue comments ---------------------------------------------------

    def _build_comments(
        self,
        *,
        commenters: list[Node],
        client: LLMClient,
        ctx: ProducerContext,
        roster: Roster,
        issue_type: str,
        title: str,
        event: Event,
    ) -> list[dict[str, object]]:
        """Turn the issue's reviewers into grounded Jira comments with in-window stamps.

        Each commenter (a real in-scope person) leaves one short grounded comment,
        stamped at an in-window offset off the event instant. Deterministic given a
        deterministic backend; out-of-scope names in a comment are still caught when
        the rendered JSON is mention-tagged.
        """
        comments: list[dict[str, object]] = []
        for i, person in enumerate(commenters):
            at = event.timestamp + _FIRST_COMMENT_DELAY + _COMMENT_SPACING * i
            text = _generate_comment_text(
                client=client,
                ctx=ctx,
                roster=roster,
                issue_type=issue_type,
                title=title,
                event=event,
                author=person,
                at=at,
            )
            comments.append(
                {
                    "author": {"displayName": _display_name(person), "accountId": person.id},
                    "created": at.isoformat(),
                    "body": text,
                }
            )
        return comments


# -- prompt assembly --------------------------------------------------------


def _system_prompt(issue_type: str) -> str:
    """Per-issue-type system prompt (cacheable across the run, §16.1)."""
    return (
        f"You write the description field of a Jira {issue_type} for an enterprise "
        "knowledge corpus. Write a concise, factual ticket description grounded "
        "strictly in the supplied context. Refer only to the people and entities "
        "named in the roster, by exactly those names. Do not invent names, dates, "
        "or facts."
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
    issue_type: str,
    title: str,
    roster: Roster,
    reporter: Node | None,
    candidates: Sequence[str],
) -> str:
    """The volatile per-issue suffix: brief + roster + templated facts (§16.1)."""
    topic = str(event.payload.get("topic") or event.payload.get("intent") or title)
    lines = [
        f"Task: write the description of a Jira {issue_type} (deliverable kind {kind!r}).",
        f"Summary: {title}.",
        f"Raised: {event.timestamp.date().isoformat()}.",
        f"Topic: {topic}.",
    ]
    if reporter is not None:
        lines.append(f"Reported by: {_display_name(reporter)}.")
    lines.append("")
    lines.append(roster.roster_block())
    if candidates:
        lines.append("")
        lines.append(
            "You may cite any of these prior artifacts by id, and you MUST report "
            "the ids you cite in references_used: " + ", ".join(candidates)
        )
    return "\n".join(lines)


def _repair_prompt(system: str, stable: list[str], brief: str, unresolved: Sequence[str]) -> Prompt:
    """Re-assemble the prompt with a corrective instruction (the single repair, D30.3)."""
    correction = (
        "\n\nCORRECTION: your previous draft named "
        f"{', '.join(unresolved)}, which is not in scope. Rewrite using ONLY the "
        "roster names above; remove or replace every other name."
    )
    return assemble_prompt(system=system, stable_context=stable, brief=brief + correction)


def _generate_comment_text(
    *,
    client: LLMClient,
    ctx: ProducerContext,
    roster: Roster,
    issue_type: str,
    title: str,
    event: Event,
    author: Node,
    at: datetime,
) -> str:
    """Generate one grounded, in-character Jira comment (a single constrained call)."""
    system = (
        f"You write a single brief Jira comment (one or two sentences) on a "
        f"{issue_type}. Write as the named teammate, grounded strictly in the "
        "supplied context. Refer only to people and entities in the roster, by "
        "exactly those names. Do not invent names, dates, or facts."
    )
    topic = str(event.payload.get("topic") or event.payload.get("intent") or title)
    lines = [
        f"Write {_display_name(author)}'s comment on the {issue_type} titled {title!r}.",
        f"Topic: {topic}.",
        f"Date: {at.date().isoformat()}.",
        "",
        roster.roster_block(),
    ]
    prompt = assemble_prompt(
        system=system, stable_context=_stable_context(ctx), brief="\n".join(lines)
    )
    result = client.generate_content(prompt)
    return " ".join(result.content.split()).strip()


# -- issue JSON rendering ---------------------------------------------------


def _render_issue_json(
    *,
    issue_key: str,
    issue_type: str,
    kind: str,
    title: str,
    description: str,
    event: Event,
    reporter: Node | None,
    assignee: Node | None,
    references: Sequence[str],
    comments: Sequence[dict[str, object]],
    view: WorldView,
) -> str:
    """Render the Jira-style issue JSON (the on-disk artifact + tagger input).

    The shape mirrors the Jira REST ``issue`` resource. Everything but the
    ``description`` and comment bodies is *templated* from bound roles and verified
    citations, so the structured fields are grounded by construction (§16.2.2).
    Output is deterministic and pretty-printed for review; concatenating the
    one-line form of many issues yields a Jira ``ndjson`` export.
    """
    project = _project_node(event, view)
    fields: dict[str, object] = {
        "summary": title,
        "issuetype": {"name": issue_type},
        "status": {"name": _status_for(kind)},
        "priority": {"name": _priority_for(event)},
        "labels": _labels(kind, event),
        "created": event.timestamp.isoformat(),
        "updated": _updated(event, comments),
        "description": description.strip(),
    }
    if project is not None:
        fields["project"] = {
            "key": _project_key(project),
            "name": _display_name(project),
            "id": project.id,
        }
    if reporter is not None:
        fields["reporter"] = {"displayName": _display_name(reporter), "accountId": reporter.id}
    if assignee is not None:
        fields["assignee"] = {"displayName": _display_name(assignee), "accountId": assignee.id}
    links = _issue_links(references, view)
    if links:
        fields["issuelinks"] = links
    if comments:
        fields["comment"] = {"comments": list(comments), "total": len(comments)}

    issue = {"key": issue_key, "fields": fields}
    return json.dumps(issue, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _issue_links(references: Sequence[str], view: WorldView) -> list[dict[str, object]]:
    """Map verified references to Jira ``relates to`` issue links (templated, D16)."""
    links: list[dict[str, object]] = []
    for ref in references:
        node = view.get_node(ref)
        label = node.props.get("title", ref) if node is not None else ref
        links.append(
            {
                "type": {"name": "Relates", "outward": "relates to"},
                "outwardIssue": {"key": ref, "summary": label},
            }
        )
    return links


# -- KG node/edge construction ----------------------------------------------


def _artifact_node(
    *,
    artifact_id: str,
    kind: str,
    medium: str,
    issue_type: str,
    issue_key: str,
    title: str,
    event: Event,
    reporter: Node | None,
    commenters: list[Node],
    path: str,
) -> Node:
    """Build (or re-declare) the ``Artifact`` node this issue renders (§3)."""
    return Node(
        id=artifact_id,
        type=_N_ARTIFACT,
        created_at=event.timestamp,
        props={
            "kind": kind,
            "medium": medium,
            "format": "jira",
            "issue_type": issue_type,
            "issue_key": issue_key,
            "title": title,
            "path": path,
            "date": event.timestamp.date().isoformat(),
            "reporter": reporter.id if reporter is not None else None,
            "commenters": [n.id for n in commenters],
            "event": event.id,
        },
        aliases=[title, issue_key],
    )


def _build_edges(
    *,
    artifact_id: str,
    event: Event,
    view: WorldView,
    reporter: Node | None,
    commenters: list[Node],
    references: Sequence[str],
) -> list[Edge]:
    """Build the reified relationships the issue expresses (§3, §11.3, D16)."""
    edges: list[Edge] = []
    ts = event.timestamp
    if reporter is not None:
        edges.append(_edge(_E_AUTHORED, reporter.id, artifact_id, ts))
    for commenter in commenters:
        edges.append(_edge(_E_REVIEWED, commenter.id, artifact_id, ts))
    for subject in event.subjects:
        if subject == artifact_id or view.get_node(subject) is None:
            continue
        edges.append(_edge(_E_EXPRESSES, artifact_id, subject, ts))
    for ref in references:
        if view.get_node(ref) is None:
            continue
        edges.append(_edge(_E_REFERENCES, artifact_id, ref, ts))
    return edges


def _edge(edge_type: str, src: str, dst: str, ts: datetime) -> Edge:
    return Edge(
        id=f"edge:{edge_type}:{src}->{dst}", type=edge_type, src=src, dst=dst, created_at=ts
    )


def _metadata(
    *,
    artifact_id: str,
    kind: str,
    medium: str,
    issue_type: str,
    issue_key: str,
    title: str,
    path: str,
    event: Event,
    reporter: Node | None,
    assignee: Node | None,
    commenters: list[Node],
    references: Sequence[str],
    comments: Sequence[dict[str, object]],
    mentions: Sequence[Mention],
    issues: Sequence[ValidationIssue],
) -> dict[str, object]:
    """The JSON twin of the artifact (the ``json`` deliverable, §11.4)."""
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "medium": medium,
        "format": "jira",
        "issue_type": issue_type,
        "issue_key": issue_key,
        "title": title,
        "path": path,
        "event": event.id,
        "date": event.timestamp.date().isoformat(),
        "reporter": reporter.id if reporter is not None else None,
        "assignee": assignee.id if assignee is not None else None,
        "commenters": [n.id for n in commenters],
        "references": list(references),
        "comments": [
            {"author": c["author"]["accountId"], "created": c["created"]}  # type: ignore[index]
            for c in comments
        ],
        "mentions": [m.to_dict() for m in mentions],
        "issues": [i.to_dict() for i in issues],
    }


# -- people resolution ------------------------------------------------------


def _resolve_people(event: Event, view: WorldView, roles: Sequence[str]) -> list[Node]:
    """Resolve the in-scope person nodes bound to ``roles`` (de-duplicated, first-seen)."""
    seen: set[str] = set()
    people: list[Node] = []
    for role in roles:
        for pid in event.actors.get(role, []):
            if pid in seen:
                continue
            seen.add(pid)
            node = view.get_node(pid)
            if node is not None and node.type == "Person":
                people.append(node)
    return people


def _reporter(event: Event, view: WorldView) -> Node | None:
    """The issue reporter: a preferred role, else *any* first in-scope actor.

    Real playbook events key actors by process-role name (``lead`` / ``engineers``),
    not ``author``; falling back to the first actor of any role keeps the reporter
    a real person — and the ``authored`` edge well-formed — regardless of which step
    raised the ticket.
    """
    preferred = _resolve_people(event, view, _REPORTER_ROLES)
    if preferred:
        return preferred[0]
    for ids in event.actors.values():
        for pid in ids:
            node = view.get_node(pid)
            if node is not None and node.type == "Person":
                return node
    return None


def _assignee(event: Event, view: WorldView, reporter: Node | None) -> Node | None:
    """The assignee: a preferred role distinct from the reporter, else the reporter."""
    for candidate in _resolve_people(event, view, _ASSIGNEE_ROLES):
        if reporter is None or candidate.id != reporter.id:
            return candidate
    return reporter


# -- field derivation -------------------------------------------------------


def _issue_type_for(kind: str) -> str:
    return _ISSUE_TYPES.get(kind, "Task")


def _status_for(kind: str) -> str:
    return "Backlog" if kind in {"backlog", "epic"} else "To Do"


def _priority_for(event: Event) -> str:
    priority = event.payload.get("priority")
    if isinstance(priority, str) and priority:
        return priority.title()
    return "Medium"


def _labels(kind: str, event: Event) -> list[str]:
    labels = ["enterprise-sim", _slug(kind)]
    initiative = event.initiative or event.payload.get("initiative")
    if isinstance(initiative, str) and initiative:
        labels.append(_slug(initiative))
    # De-duplicate preserving order so the field is stable + line-diffable.
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _updated(event: Event, comments: Sequence[dict[str, object]]) -> str:
    """The ``updated`` stamp: the last comment's time, else the raised instant."""
    if comments:
        return str(comments[-1]["created"])
    return event.timestamp.isoformat()


def _project_node(event: Event, view: WorldView) -> Node | None:
    """The project the issue belongs to: ``event.project`` else a Project subject."""
    if event.project:
        node = view.get_node(event.project)
        if node is not None and node.type == _N_PROJECT:
            return node
    for subject in event.subjects:
        node = view.get_node(subject)
        if node is not None and node.type == _N_PROJECT:
            return node
    return None


def _project_key(project: Node) -> str:
    """A Jira project key from the project's name (initials of its words, upper)."""
    name = _display_name(project)
    initials = "".join(word[0] for word in re.split(r"[^A-Za-z0-9]+", name) if word)
    token = re.sub(r"[^A-Za-z0-9]", "", initials).upper()[:4]
    return token or _DEFAULT_PROJECT_KEY


def _issue_key(event: Event, view: WorldView) -> str:
    """A deterministic ``PROJ-<n>`` Jira key, stable in the event id (D10)."""
    project = _project_node(event, view)
    token = _project_key(project) if project is not None else _DEFAULT_PROJECT_KEY
    digest = hashlib.sha1(event.id.encode("utf-8")).hexdigest()
    number = int(digest[:6], 16) % 9000 + 100
    return f"{token}-{number}"


# -- small helpers ----------------------------------------------------------


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
    """Mint a modality-discriminated artifact id so the jira twin never collides.

    A fan-out (``backlog`` → ``markdown`` + ``jira``) renders one event with two
    producers; giving the jira artifact a ``…:jira`` id keeps it a distinct
    ``Artifact`` node from its markdown twin (which keeps the bare id), so both
    survive :func:`~enterprise_sim.producers.artifact.apply_to_world`.
    """
    for subject in event.subjects:
        if subject.startswith(("artifact:", "art:")):
            return f"{subject}:jira"
    return f"artifact:{event.id.split(':', 1)[-1]}:jira"


def _slug(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return out or "artifact"


# Self-register so the binding map can resolve "jira" (registration fires on import,
# mirroring the word/pptx producers); guarded so re-import in tests is idempotent.
if "jira" not in PRODUCERS:
    PRODUCERS.register(JiraProducer())
