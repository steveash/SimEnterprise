"""The ``word`` producer — events → grounded ``.docx`` with native threaded comments (§4, §9, §16).

The third concrete producer (after ``markdown`` and ``outlook``, §16): it satisfies
the same :class:`~enterprise_sim.core.registry.plugins.Producer` protocol and is a
pure function of ``(event, view)``. The binding map rebinds the document
deliverable kinds (``status_report``, ``design_doc``) here; everything else stays
with the ``markdown`` default. A word artifact renders the same grounded prose its
markdown twin would — generated against a constrained
:class:`~enterprise_sim.producers.grounding.Roster` with templated
author/reviewer/date/reference facts and a single detect-and-repair pass (D30) —
and serializes it as an OOXML ``.docx`` package via
:mod:`enterprise_sim.producers.word_docx`.

What makes the word artifact distinctive is **native threaded comments**. A
document's reviewers (resolved to real :class:`~enterprise_sim.core.world.Node`
people) each leave a grounded review comment, anchored to a span of the body and
threaded into a reply chain with in-window timestamps derived from the draft —
so the rendered ``.docx`` opens with a genuine Word reply thread, not flattened
prose. Each comment's prose is generated grounded in the same roster.

The ``.docx`` bytes ride in :attr:`ProducedArtifact.binary_body`; ``body`` carries
the plain-text projection (body + thread) the tagger runs over. The KG
node/edges/metadata mirror the markdown producer's shape, so the corpus stays
cross-modally KG-consistent. Output is deterministic given a deterministic
backend (D10) — the ``.docx`` builder uses a fixed zip epoch and the ``word``
producer is otherwise a pure function of its inputs.
"""

from __future__ import annotations

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
from enterprise_sim.producers.word_docx import DocxComment, DocxDocument, build_docx

__all__ = ["WordProducer"]

# KG node/edge type constants this producer reads and writes (§3).
_N_ARTIFACT = "Artifact"
_E_AUTHORED = "authored"
_E_REVIEWED = "reviewed"
_E_EXPRESSES = "expresses"
_E_REFERENCES = "references"

_AUTHOR_ROLES = ("author", "authors")
_REVIEWER_ROLES = ("reviewer", "reviewers")

# How long after the draft the review thread opens, and the cadence between
# successive comments — small, in-window offsets derived from the draft instant so
# timestamps are deterministic and sit inside the document's review window.
_FIRST_COMMENT_DELAY = timedelta(hours=1)
_COMMENT_SPACING = timedelta(minutes=45)


class WordProducer:
    """Render an :class:`Event`'s document deliverable to a ``.docx`` with native comments.

    Satisfies the :class:`~enterprise_sim.core.registry.plugins.Producer` protocol
    (``name`` / ``formats`` / ``handles``). It is *not* a catch-all — it handles the
    document kinds the binding map rebinds to it; everything else stays with
    ``markdown``.
    """

    name = "word"
    formats: Sequence[str] = ("docx",)
    handles: Sequence[str] = ("status_report", "design_doc")

    def produce(
        self,
        event: Event,
        view: WorldView,
        client: LLMClient,
        ctx: ProducerContext | None = None,
    ) -> ProducedArtifact:
        """Render ``event`` against ``view`` and return its :class:`ProducedArtifact`.

        The reviewers bound to ``event`` become a native threaded comment chain
        attributed to those real people, with in-window timestamps. Nothing is
        written to disk and ``view`` is not mutated — the caller applies the result
        via :func:`~enterprise_sim.producers.artifact.apply_to_world`.
        """
        ctx = ctx or ProducerContext()
        roster = Roster.from_worldview(view)

        kind = event.deliverable.kind if event.deliverable else "document"
        medium = event.deliverable.medium if event.deliverable else "document"
        title = _title_for(event, kind)
        artifact_id = _artifact_id_for(event)
        path = f"{ctx.artifacts_dir}/{_slug(artifact_id)}.docx"

        authors = _resolve_people(event, view, _AUTHOR_ROLES)
        reviewers = _resolve_people(event, view, _REVIEWER_ROLES)
        candidates = _candidate_references(view, exclude=artifact_id)

        prose, references, issues = self._generate_grounded(
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

        paragraphs, anchor_idx = _body_paragraphs(
            title=title,
            kind=kind,
            event=event,
            authors=authors,
            reviewers=reviewers,
            prose=prose,
            references=references,
            view=view,
        )
        thread = self._build_thread(
            reviewers=reviewers,
            client=client,
            ctx=ctx,
            roster=roster,
            kind=kind,
            title=title,
            event=event,
        )
        docx = DocxDocument(
            body=paragraphs,
            comments=thread,
            anchor=paragraphs[anchor_idx],
            anchor_paragraph=anchor_idx,
        )
        binary_body = build_docx(docx)

        # Plain-text projection: the document body plus the rendered thread, so the
        # tagger records mentions across both the prose and the comments (§11.3).
        projection = _projection(paragraphs, thread)
        mentions = tag_mentions(projection, roster, artifact_path=path, medium="docx")

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
            references=references,
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
            references=references,
            mentions=mentions,
            issues=issues,
            thread=thread,
        )
        return ProducedArtifact(
            artifact_id=artifact_id,
            path=path,
            fmt="docx",
            body=projection,
            binary_body=binary_body,
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
        candidates: Sequence[str],
        ctx: ProducerContext,
        path: str,
    ) -> tuple[str, tuple[str, ...], list[ValidationIssue]]:
        """Generate the body prose, then run the detect + single-repair loop (D30.3).

        Mirrors the markdown producer's single-repair loop: a draft that names an
        out-of-scope entity is regenerated once with a corrective instruction;
        whatever is still unresolved becomes a validation issue and the artifact is
        kept (§16.2.3 / D17).
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
            repair = _repair_prompt(system, stable, brief, unresolved)
            repaired = client.generate_content(repair, candidate_references=candidates)
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

    # -- native threaded comments -----------------------------------------

    def _build_thread(
        self,
        *,
        reviewers: list[Node],
        client: LLMClient,
        ctx: ProducerContext,
        roster: Roster,
        kind: str,
        title: str,
        event: Event,
    ) -> list[DocxComment]:
        """Turn the document's reviewers into a native, threaded review chain.

        Each reviewer (a real in-scope person) leaves one grounded comment; the
        comments form a reply chain (the first is top-level, each later one replies
        to its predecessor) with in-window timestamps spaced off the draft instant.
        Deterministic given a deterministic backend.
        """
        thread: list[DocxComment] = []
        for i, person in enumerate(reviewers):
            at = event.timestamp + _FIRST_COMMENT_DELAY + _COMMENT_SPACING * i
            text = _generate_comment_text(
                client=client,
                ctx=ctx,
                roster=roster,
                kind=kind,
                title=title,
                event=event,
                author=person,
                at=at,
                replying=i > 0,
            )
            name = _display_name(person)
            thread.append(
                DocxComment(
                    author=name,
                    initials=_initials(name),
                    text=text,
                    timestamp=at,
                    parent=i - 1 if i > 0 else None,
                )
            )
        return thread


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
    kind: str,
    title: str,
    event: Event,
    author: Node,
    at: datetime,
    replying: bool,
) -> str:
    """Generate one grounded, in-character review comment (a single constrained call).

    The comment is short and roster-grounded; unlike the body it gets no repair
    pass — a comment is low-stakes, and any out-of-scope name in it is still caught
    when the projection is mention-tagged. Deterministic given a deterministic
    backend.
    """
    human = kind.replace("_", " ")
    system = (
        f"You write a single brief peer-review comment (one or two sentences) on a "
        f"{human}. Write as the named reviewer, grounded strictly in the supplied "
        "context. Refer only to people and entities in the roster, by exactly those "
        "names. Do not invent names, dates, or facts."
    )
    topic = str(event.payload.get("topic") or event.payload.get("intent") or title)
    lines = [
        f"Write {_display_name(author)}'s review comment on the {human} titled {title!r}.",
        f"Topic: {topic}.",
        f"Date: {at.date().isoformat()}.",
        (
            "This comment replies to an earlier comment in the thread."
            if replying
            else "This is a top-level comment on the document."
        ),
        "",
        roster.roster_block(),
    ]
    prompt = assemble_prompt(
        system=system, stable_context=_stable_context(ctx), brief="\n".join(lines)
    )
    result = client.generate_content(prompt)
    return " ".join(result.content.split()).strip()


# -- body + projection rendering --------------------------------------------


def _body_paragraphs(
    *,
    title: str,
    kind: str,
    event: Event,
    authors: list[Node],
    reviewers: list[Node],
    prose: str,
    references: Sequence[str],
    view: WorldView,
) -> tuple[list[str], int]:
    """Build the docx body paragraphs and the index of the comment-anchor paragraph.

    The header (title + a metadata line) and the references list are templated from
    bound roles + verified citations (§16.2.2). The anchor is the first prose
    paragraph (the substring the comment thread attaches to), falling back to the
    title when the prose is empty.
    """
    paragraphs: list[str] = [title]
    meta = [f"Type: {kind.replace('_', ' ')}", f"Date: {event.timestamp.date().isoformat()}"]
    if authors:
        meta.append(f"Author{'s' if len(authors) > 1 else ''}: {_names(authors)}")
    if reviewers:
        meta.append(f"Reviewers: {_names(reviewers)}")
    paragraphs.append(" | ".join(meta))

    prose_paras = _split_paragraphs(prose)
    anchor_idx = len(paragraphs)
    if prose_paras:
        paragraphs.extend(prose_paras)
    else:
        anchor_idx = 0  # fall back to the title span

    if references:
        paragraphs.append("References")
        for ref in references:
            node = view.get_node(ref)
            label = node.props.get("title", ref) if node is not None else ref
            paragraphs.append(f"- {label} ({ref})")
    return paragraphs, anchor_idx


def _split_paragraphs(prose: str) -> list[str]:
    """Split prose into paragraphs on blank lines; collapse intra-paragraph newlines."""
    out: list[str] = []
    for block in prose.strip().split("\n\n"):
        text = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if text:
            out.append(text)
    return out


def _projection(paragraphs: Sequence[str], thread: Sequence[DocxComment]) -> str:
    """The plain-text twin of the docx: the body followed by the comment thread."""
    lines = list(paragraphs)
    if thread:
        lines.append("")
        lines.append("Comments")
        for c in thread:
            lines.append(f"{c.author} ({c.timestamp.date().isoformat()}): {c.text}")
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
        type=_N_ARTIFACT,
        created_at=event.timestamp,
        props={
            "kind": kind,
            "medium": medium,
            "format": "docx",
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
        edges.append(_edge(_E_AUTHORED, author.id, artifact_id, ts))
    for reviewer in reviewers:
        edges.append(_edge(_E_REVIEWED, reviewer.id, artifact_id, ts))
    for subject in event.subjects:
        if subject == artifact_id or view.get_node(subject) is None:
            continue
        edges.append(_edge(_E_EXPRESSES, artifact_id, subject, ts))
    for ref in references:
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
    title: str,
    path: str,
    event: Event,
    authors: list[Node],
    reviewers: list[Node],
    references: Sequence[str],
    mentions: Sequence[Mention],
    issues: Sequence[ValidationIssue],
    thread: Sequence[DocxComment],
) -> dict[str, object]:
    """The JSON twin of the artifact (the ``json`` deliverable, §11.4)."""
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "medium": medium,
        "format": "docx",
        "title": title,
        "path": path,
        "event": event.id,
        "date": event.timestamp.date().isoformat(),
        "authors": [n.id for n in authors],
        "reviewers": [n.id for n in reviewers],
        "references": list(references),
        "mentions": [m.to_dict() for m in mentions],
        "issues": [i.to_dict() for i in issues],
        "comments": [
            {"author": c.author, "date": c.timestamp.isoformat(), "parent": c.parent}
            for c in thread
        ],
    }


# -- small helpers ----------------------------------------------------------


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


def _candidate_references(view: WorldView, *, exclude: str) -> list[str]:
    """Recent in-scope artifact ids the model may cite (the candidate set, §16.3)."""
    return [n.id for n in view.nodes_by_type(_N_ARTIFACT) if n.id != exclude]


def _names(nodes: Sequence[Node]) -> str:
    return ", ".join(_display_name(n) for n in nodes)


def _display_name(node: Node) -> str:
    name = node.props.get("name") or node.props.get("title")
    return str(name) if name else node.id.split(":", 1)[-1]


def _initials(name: str) -> str:
    """Up to three uppercase initials from a display name (``Ada Lovelace`` → ``AL``)."""
    letters = [word[0].upper() for word in name.split() if word]
    return "".join(letters[:3]) or name[:1].upper()


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
    out = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return out or "artifact"


# Self-register so the binding map can resolve "word" (registration fires on import,
# mirroring the pptx producer); guarded so re-import in tests is idempotent.
if "word" not in PRODUCERS:
    PRODUCERS.register(WordProducer())
