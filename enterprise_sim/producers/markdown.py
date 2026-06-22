"""The ``markdown`` producer — events → grounded markdown artifacts (ARCHITECTURE.md §16, §11.3).

This is the v1 default producer: the binding map routes *every* deliverable kind
here (``word``/``pptx`` rebind specific kinds later, §4). It turns one
:class:`~enterprise_sim.core.events.Event` and the timestamped
:class:`~enterprise_sim.core.world.WorldView` it fires against into a single
rendered markdown file plus the knowledge-graph facts that file expresses, with
all three grounding layers applied (D30):

1. **Constrained input** — a :class:`~enterprise_sim.producers.grounding.Roster`
   built from the view restricts what the model may name, and is injected into the
   prompt as an explicit roster block (§16.2.1).
2. **Templated references** — the author line, reviewer list, date, and citations
   are filled from the event's bound roles and verified ``references_used``, never
   generated (§16.2.2). Only the body prose is LLM-written.
3. **Detect + one repair** — the mention tagger scans the prose; an unresolved
   name-like token triggers a *single* repair re-prompt, and anything still
   unresolved becomes a ``validation/issues.jsonl`` entry while the artifact is
   kept (§16.2.3 / D17).

Generation uses :meth:`LLMClient.generate_content` (§16.3, D32): the candidate
reference set is the recent in-scope artifacts of the view, the model reports
which it cited, the client verifies those against the candidate set, and we turn
the survivors into ``references`` edges (D16). The producer is otherwise a pure
function of ``(event, view)`` — deterministic given a deterministic backend.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from enterprise_sim.core.events import Event
from enterprise_sim.core.llm import LLMClient, Prompt, assemble_prompt
from enterprise_sim.core.world import Edge, Node, WorldView
from enterprise_sim.producers.artifact import (
    Mention,
    ProducedArtifact,
    ValidationIssue,
)
from enterprise_sim.producers.grounding import (
    Roster,
    detect_unresolved_names,
    tag_mentions,
)

__all__ = ["MarkdownProducer", "ProducerContext"]

# KG node/edge type constants this producer reads and writes (§3).
N_ARTIFACT = "Artifact"
E_AUTHORED = "authored"
E_REVIEWED = "reviewed"
E_EXPRESSES = "expresses"
E_REFERENCES = "references"

# Role names in ``event.actors`` that map to the author / reviewer templated lines.
_AUTHOR_ROLES = ("author", "authors")
_REVIEWER_ROLES = ("reviewer", "reviewers")


@dataclass(frozen=True, slots=True)
class ProducerContext:
    """Shared, cache-friendly context a producer renders against (§16.1).

    ``company_profile`` and ``scenario_context`` are the stable prompt prefix
    blocks (company-wide / per-scenario) that prompt caching amortizes across a
    run; both are optional so the producer works standalone in tests. ``artifacts_dir``
    is the run-relative directory rendered files land under.
    """

    company_profile: str = ""
    scenario_context: str = ""
    artifacts_dir: str = "artifacts"


class MarkdownProducer:
    """Render an :class:`Event`'s deliverable to a grounded markdown artifact.

    Satisfies the :class:`~enterprise_sim.core.registry.plugins.Producer` protocol
    (``name`` / ``formats`` / ``handles``); ``handles=("*",)`` marks it the v1
    catch-all the binding map uses as its ``default``.
    """

    name = "markdown"
    formats: Sequence[str] = ("markdown",)
    handles: Sequence[str] = ("*",)

    def produce(
        self,
        event: Event,
        view: WorldView,
        client: LLMClient,
        ctx: ProducerContext | None = None,
    ) -> ProducedArtifact:
        """Render ``event`` against ``view`` and return its :class:`ProducedArtifact`.

        The returned object carries the rendered body, the ``Artifact`` node, the
        ``authored`` / ``reviewed`` / ``expresses`` / ``references`` edges, the
        tagged mentions, any validation issues, and a JSON metadata twin. Nothing
        is written to disk and ``view`` is not mutated — the caller applies the
        result via :func:`~enterprise_sim.producers.artifact.apply_to_world`.
        """
        ctx = ctx or ProducerContext()
        roster = Roster.from_worldview(view)

        kind = event.deliverable.kind if event.deliverable else "document"
        medium = event.deliverable.medium if event.deliverable else "document"
        title = _title_for(event, kind)
        artifact_id = _artifact_id_for(event)
        path = f"{ctx.artifacts_dir}/{_slug(artifact_id)}.md"

        authors = _resolve_people(event, view, _AUTHOR_ROLES)
        reviewers = _resolve_people(event, view, _REVIEWER_ROLES)
        candidates = _candidate_references(view, exclude=artifact_id)

        # --- generate prose, then detect + (at most one) repair (§16.2.3) -----
        prose, references_used, issues = self._generate_grounded(
            client=client,
            event=event,
            kind=kind,
            title=title,
            roster=roster,
            authors=authors,
            reviewers=reviewers,
            candidates=candidates,
            ctx=ctx,
            path=path,
        )

        body = _render_markdown(
            title=title,
            kind=kind,
            event=event,
            authors=authors,
            reviewers=reviewers,
            prose=prose,
            references=references_used,
            view=view,
        )
        mentions = tag_mentions(body, roster, artifact_path=path)

        node = _artifact_node(
            artifact_id=artifact_id,
            kind=kind,
            medium=medium,
            title=title,
            event=event,
            authors=authors,
            reviewers=reviewers,
            path=path,
        )
        edges = _build_edges(
            artifact_id=artifact_id,
            event=event,
            view=view,
            authors=authors,
            reviewers=reviewers,
            references=references_used,
        )
        metadata = _metadata(
            artifact_id=artifact_id,
            kind=kind,
            medium=medium,
            title=title,
            path=path,
            event=event,
            authors=authors,
            reviewers=reviewers,
            references=references_used,
            mentions=mentions,
            issues=issues,
        )
        return ProducedArtifact(
            artifact_id=artifact_id,
            path=path,
            fmt="markdown",
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
        authors: list[Node],
        reviewers: list[Node],
        candidates: list[str],
        ctx: ProducerContext,
        path: str,
    ) -> tuple[str, tuple[str, ...], list[ValidationIssue]]:
        """Generate prose, then run the detect + single-repair loop (D30.3).

        Returns ``(prose, verified_references, issues)``. A repair is attempted at
        most once: if the first draft names an out-of-scope entity, a corrective
        instruction is appended and the draft regenerated. Whatever is still
        unresolved after that is reported as a validation issue, never raised.
        """
        system = _system_prompt(kind)
        stable = _stable_context(ctx)
        brief = _task_brief(
            event=event,
            kind=kind,
            title=title,
            roster=roster,
            authors=authors,
            reviewers=reviewers,
            candidates=candidates,
        )
        prompt = assemble_prompt(system=system, stable_context=stable, brief=brief)
        result = client.generate_content(prompt, candidate_references=candidates)
        prose = result.content
        references = result.references_used

        unresolved = detect_unresolved_names(prose, roster)
        if unresolved:
            repair_prompt = _repair_prompt(system, stable, brief, roster, unresolved)
            repaired = client.generate_content(repair_prompt, candidate_references=candidates)
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


def _system_prompt(kind: str) -> str:
    """Per-artifact-kind system prompt (cacheable across the run, §16.1)."""
    human = kind.replace("_", " ")
    return (
        f"You write the body of a {human} for an enterprise knowledge corpus. "
        "Write clear, factual prose grounded strictly in the supplied context. "
        "Refer only to the people and entities named in the roster, by exactly "
        "those names. Do not invent names, dates, or facts."
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
    authors: list[Node],
    reviewers: list[Node],
    candidates: Sequence[str],
) -> str:
    """The volatile per-artifact suffix: brief + roster + templated facts (§16.1)."""
    topic = str(event.payload.get("topic") or event.payload.get("intent") or title)
    tone = event.payload.get("tone")
    lines = [
        f"Task: write the prose body of a {kind.replace('_', ' ')}.",
        f"Title: {title}.",
        f"Date: {event.timestamp.date().isoformat()}.",
        f"Topic: {topic}.",
    ]
    if tone:
        lines.append(f"Tone: {tone}.")
    if authors:
        lines.append(f"Authored by: {_names(authors)}.")
    if reviewers:
        lines.append(f"Reviewed by: {_names(reviewers)}.")
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
    roster: Roster,
    unresolved: Sequence[str],
) -> Prompt:
    """Re-assemble the prompt with a corrective instruction (the single repair, D30.3)."""
    correction = (
        "\n\nCORRECTION: your previous draft named "
        f"{', '.join(unresolved)}, which is not in scope. Rewrite using ONLY the "
        "roster names above; remove or replace every other name."
    )
    return assemble_prompt(system=system, stable_context=stable, brief=brief + correction)


# -- markdown rendering -----------------------------------------------------


def _render_markdown(
    *,
    title: str,
    kind: str,
    event: Event,
    authors: list[Node],
    reviewers: list[Node],
    prose: str,
    references: Sequence[str],
    view: WorldView,
) -> str:
    """Render the final markdown: templated header + LLM prose + templated refs.

    The header (title, kind, author/reviewer/date lines) and the references list
    are *templated* from bound roles + verified citations, never generated, so
    they are grounded by construction (§16.2.2). Output is deterministic.
    """
    lines = [f"# {title}", ""]
    meta = [
        f"**Type:** {kind.replace('_', ' ')}",
        f"**Date:** {event.timestamp.date().isoformat()}",
    ]
    if authors:
        meta.append(f"**Author{'s' if len(authors) > 1 else ''}:** {_names(authors)}")
    if reviewers:
        meta.append(f"**Reviewers:** {_names(reviewers)}")
    lines.append(" · ".join(meta))
    lines += ["", prose.strip(), ""]
    if references:
        lines += ["## References", ""]
        for ref in references:
            node = view.get_node(ref)
            label = node.props.get("title", ref) if node is not None else ref
            lines.append(f"- [{label}]({ref})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- KG node/edge construction ----------------------------------------------


def _artifact_node(
    *,
    artifact_id: str,
    kind: str,
    medium: str,
    title: str,
    event: Event,
    authors: list[Node],
    reviewers: list[Node],
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
            "authors": [n.id for n in authors],
            "reviewers": [n.id for n in reviewers],
            "event": event.id,
        },
        aliases=[title],
    )


def _build_edges(
    *,
    artifact_id: str,
    event: Event,
    view: WorldView,
    authors: list[Node],
    reviewers: list[Node],
    references: Sequence[str],
) -> list[Edge]:
    """Build the reified relationships the artifact expresses (§3, §11.3, D16)."""
    edges: list[Edge] = []
    ts = event.timestamp
    for author in authors:
        edges.append(_edge(E_AUTHORED, author.id, artifact_id, ts))
    for reviewer in reviewers:
        edges.append(_edge(E_REVIEWED, reviewer.id, artifact_id, ts))
    # Provenance: this file is evidence for the subjects it is about (§11.3).
    for subject in event.subjects:
        if subject == artifact_id or view.get_node(subject) is None:
            continue
        edges.append(_edge(E_EXPRESSES, artifact_id, subject, ts))
    # references edges from verified citations (D16/D32).
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
    title: str,
    path: str,
    event: Event,
    authors: list[Node],
    reviewers: list[Node],
    references: Sequence[str],
    mentions: Sequence[Mention],
    issues: Sequence[ValidationIssue],
) -> dict[str, object]:
    """The JSON twin of the artifact (the ``markdown/json`` deliverable)."""
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "medium": medium,
        "title": title,
        "path": path,
        "event": event.id,
        "date": event.timestamp.date().isoformat(),
        "authors": [n.id for n in authors],
        "reviewers": [n.id for n in reviewers],
        "references": list(references),
        "mentions": [m.to_dict() for m in mentions],
        "issues": [i.to_dict() for i in issues],
    }


# -- small helpers ----------------------------------------------------------


def _resolve_people(event: Event, view: WorldView, roles: Sequence[str]) -> list[Node]:
    """Resolve the person nodes bound to ``roles`` in ``event.actors`` (in-scope only).

    Ids are de-duplicated preserving first-seen order; an id absent from the view
    is skipped (a producer can only name in-scope people — that is the point of
    constrained input), so the templated lines never reference a phantom person.
    """
    seen: set[str] = set()
    people: list[Node] = []
    for role in roles:
        for pid in event.actors.get(role, []):
            if pid in seen:
                continue
            seen.add(pid)
            node = view.get_node(pid)
            if node is not None:
                people.append(node)
    return people


def _candidate_references(view: WorldView, *, exclude: str) -> list[str]:
    """Recent in-scope artifact ids the model may cite (the candidate set, §16.3)."""
    return [n.id for n in view.nodes_by_type(N_ARTIFACT) if n.id != exclude]


def _names(nodes: Sequence[Node]) -> str:
    return ", ".join(_display_name(n) for n in nodes)


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
