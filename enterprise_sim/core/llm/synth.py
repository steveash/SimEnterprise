"""The ``synth`` backend: deterministic, fact-bearing prose without a model.

The ``fake`` backend echoes the prompt, so a fake world's documents carry its
structure (who wrote what, when, citing what) but no facts. ``synth`` reads the
contract fields of the brief (:func:`~enterprise_sim.core.llm.brief.parse_brief`)
and composes prose from them:

* an opener for the document kind about the topic,
* who drafted it and with whom (only roster names the brief gave),
* **every fact of the facts block**, verbatim, woven between connective lines,
* one or two closing lines from a neutral bank.

Every choice is drawn from a seed derived from the prompt hash and the model
name, so the same world renders byte-identically, and a cached response stays
valid. No sentence of the banks holds a Title-Case word pair, so the grounding
detector never sees an out-of-roster "name" in synth prose.

``references_used`` cites at most the last two candidate artifacts (the most
recent ones the brief offered), which keeps reference chains short and local
like a person's citations rather than the fake backend's every-other pick.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from typing import Any

from enterprise_sim.core.llm.brief import ParsedBrief, parse_brief
from enterprise_sim.core.llm.prompt import Prompt
from enterprise_sim.core.llm.types import Completion, TokenUsage

__all__ = ["SynthBackend"]

#: Openers per document kind; ``{topic}`` is the brief's topic in lower case.
_OPENERS: dict[str, tuple[str, ...]] = {
    "status report": (
        "This is the weekly status update.",
        "Here is where things stand this week.",
        "Weekly update for the team and stakeholders.",
    ),
    "design doc": (
        "This document proposes how we approach {topic}.",
        "The design below covers {topic} and the trade-offs we weighed.",
    ),
    "kickoff brief": (
        "We are kicking off the work to {topic}.",
        "This brief opens the project; the goal is to {topic}.",
    ),
    "backlog": (
        "The initial backlog groups the work we know about so far.",
        "These are the backlog items raised at grooming.",
    ),
    "sprint plan": (
        "The plan for this sprint is to {topic}.",
        "This sprint we commit to the following scope.",
    ),
    "sprint review notes": (
        "Notes from the sprint review follow.",
        "The sprint closed; here is what shipped and what slipped.",
    ),
}
_GENERIC_OPENERS: tuple[str, ...] = (
    "Goal of this {kind}: {topic}.",
    "Scope of this {kind}: {topic}.",
    "Purpose: {topic}.",
)
_COMMENT_OPENERS: tuple[str, ...] = (
    "Looks reasonable overall.",
    "A few points before this goes further.",
    "Thanks for pulling this together.",
    "I have one concern here.",
)
_REPLY_OPENERS: tuple[str, ...] = (
    "Agreed, and to add to that point:",
    "Following up on the thread:",
    "Good catch.",
)
_CHAT_OPENERS: tuple[str, ...] = (
    "{posted}, see the doc for details.",
    "just shared it: {posted} ({topic}).",
    "{posted}. shout if anything looks off.",
    "{posted} - main points below.",
)
_CONNECTIVES: tuple[str, ...] = (
    "For the record:",
    "As a reminder:",
    "To be clear:",
    "Worth noting:",
    "",
    "",
    "",
    "",
)
_CLOSERS: tuple[str, ...] = (
    "Open questions are tracked in the project log.",
    "We will revisit this at the next checkpoint.",
    "Please raise any concerns before the end of the week.",
    "Risks and follow-ups are listed in the backlog.",
    "The next update will cover progress against these items.",
    "No other changes this period.",
)
_COMMENT_CLOSERS: tuple[str, ...] = (
    "Can we confirm before sign-off?",
    "Otherwise fine by me.",
    "Happy to discuss in the review.",
    "Let us capture this in the doc.",
)


def _rng(prompt: Prompt, model: str) -> random.Random:
    digest = hashlib.sha256(f"synth\x00{model}\x00{prompt.hash()}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _lower_topic(topic: str, title: str) -> str:
    text = (topic or title or "the work").strip().rstrip(".")
    return text[:1].lower() + text[1:] if text else "the work"


def _join(names: Sequence[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _fact_sentences(brief: ParsedBrief, rng: random.Random) -> list[str]:
    out: list[str] = []
    for fact in brief.facts:
        lead = rng.choice(_CONNECTIVES)
        sentence = fact if fact.endswith((".", "?", "!")) else fact + "."
        if lead:
            # A colon, not a lower-cased first letter: the fact may open with a
            # name ("Build Software Delivery ...") that must keep its case.
            sentence = f"{lead} {sentence}"
        out.append(sentence)
    return out


def compose(brief: ParsedBrief, rng: random.Random) -> str:
    """Compose the prose for ``brief`` (pure in ``brief`` and the rng state)."""
    kind = (brief.kind or "document").lower()
    topic = _lower_topic(brief.topic, brief.title)
    if kind.startswith("chat message"):
        posted = (brief.title or "an update").rstrip(".").lower()
        return rng.choice(_CHAT_OPENERS).format(posted=posted, topic=topic)
    if brief.is_comment:
        opener = rng.choice(_REPLY_OPENERS if brief.replying else _COMMENT_OPENERS)
        parts = [opener, *_fact_sentences(brief, rng), rng.choice(_COMMENT_CLOSERS)]
        return " ".join(parts)
    openers = _OPENERS.get(kind, _GENERIC_OPENERS)
    lines = [rng.choice(openers).format(topic=topic, kind=kind)]
    people = list(brief.authors)
    if people:
        helpers = [n for n in brief.reviewers if n not in people]
        line = f"{_join(people)} drafted this"
        line += f" with input from {_join(helpers)}." if helpers else "."
        lines.append(line)
    facts = _fact_sentences(brief, rng)
    body = [" ".join(lines)]
    if facts:
        # Two or three facts to a paragraph, in brief order.
        size = rng.choice((2, 3))
        body.extend(" ".join(facts[i : i + size]) for i in range(0, len(facts), size))
    closers = rng.sample(_CLOSERS, k=rng.choice((1, 2)))
    body.append(" ".join(closers))
    return "\n\n".join(body)


class SynthBackend:
    """Deterministic, network-free backend that writes the brief's facts as prose."""

    name = "synth"

    def generate_structured(
        self,
        prompt: Prompt,
        *,
        schema: Mapping[str, Any],
        model: str,
        temperature: float,
    ) -> Completion:
        # Structured world-building calls have no facts to state: reuse the
        # fake backend's schema-shaped synthesis.
        from enterprise_sim.core.llm.backends import FakeBackend

        return FakeBackend().generate_structured(
            prompt, schema=schema, model=model, temperature=temperature
        )

    def generate_content(
        self,
        prompt: Prompt,
        *,
        candidate_references: Sequence[str],
        model: str,
        temperature: float,
    ) -> Completion:
        from enterprise_sim.core.llm.backends import estimate_tokens

        rng = _rng(prompt, model)
        content = compose(parse_brief(prompt.user_text), rng)
        refs = tuple(candidate_references[-2:])
        usage = TokenUsage(
            input_tokens=estimate_tokens(prompt.user_text),
            cached_input_tokens=estimate_tokens(prompt.system_text),
            output_tokens=estimate_tokens(content),
        )
        return Completion(
            text=content,
            usage=usage,
            model=model,
            structured={"content": content, "references_used": list(refs)},
            references_used=refs,
        )
