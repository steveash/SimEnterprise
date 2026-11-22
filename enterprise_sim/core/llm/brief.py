"""The machine-readable parts of a producer's task brief.

Producers write their per-artifact brief as text (it is the volatile user layer
of the prompt, §16.1). Two parts of it are a contract rather than prose:

* the header lines every producer writes (``Task:``, ``Title:``, ``Date:``,
  ``Topic:``, ``Authored by:``, ``Reviewed by:`` and the review-comment opener
  ``Write <name>'s ... comment on the <kind> titled '<title>'.``), and
* the **facts block**: the ledger assertions this document must state
  (:mod:`enterprise_sim.facts`), one sentence per line under :data:`FACTS_HEADER`.

A live model reads them as instructions. The deterministic ``synth`` backend
(:mod:`enterprise_sim.core.llm.synth`) reads them with :func:`parse_brief` and
writes prose that states exactly those facts. Keeping the format in this one
module means a producer and the backend cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FACTS_HEADER",
    "ParsedBrief",
    "facts_block",
    "facts_block_for",
    "parse_brief",
]

FACTS_HEADER = "FACTS — state each of these in the text, in your own words but keeping every value:"

_LINE = re.compile(
    r"^(Task|Title|Summary|Subject|Date|Raised|Topic|Tone|Authored by|Reviewed by|Reported by"
    r"|Prepared by|Sent by|Organized by): (.*?)\.?$"
)
_COMMENT = re.compile(r"^Write (.+?)'s (?:review )?comment on the (.+?) titled (['\"])(.*)\3\.$")
_TASK_KIND = re.compile(
    r"write the (?:prose body|description|overview narrative|body|agenda body) "
    r"(?:of|for) an? (.+?)(?: \(|\.?$)"
)


@dataclass(frozen=True, slots=True)
class ParsedBrief:
    """The contract fields of a brief (empty strings / tuples when absent)."""

    kind: str = ""
    title: str = ""
    date: str = ""
    topic: str = ""
    authors: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()
    commenter: str = ""
    replying: bool = False
    facts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_comment(self) -> bool:
        return bool(self.commenter)


def facts_block(statements: Sequence[str]) -> str:
    """The facts block for ``statements`` (empty string when there are none)."""
    if not statements:
        return ""
    return "\n".join([FACTS_HEADER, *(f"- {s}" for s in statements)])


def facts_block_for(payload: Mapping[str, Any]) -> str:
    """The facts block for an event payload's ledger assertions (``payload['facts']``)."""
    facts = payload.get("facts") or []
    return facts_block([str(f["statement"]) for f in facts if f.get("statement")])


def _names(text: str) -> tuple[str, ...]:
    return tuple(n.strip() for n in text.replace(" and ", ", ").split(",") if n.strip())


def parse_brief(text: str) -> ParsedBrief:
    """Read the contract fields back out of a brief (tolerant of anything else)."""
    values: dict[str, str] = {}
    facts: list[str] = []
    commenter = ""
    kind = ""
    title = ""
    replying = False
    in_facts = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == FACTS_HEADER:
            in_facts = True
            continue
        if in_facts:
            if line.startswith("- "):
                facts.append(line[2:].strip())
                continue
            in_facts = False
        comment = _COMMENT.match(line)
        if comment:
            commenter, kind, title = comment.group(1), comment.group(2), comment.group(4)
            continue
        if line.startswith("This comment replies"):
            replying = True
            continue
        match = _LINE.match(line)
        if match:
            values.setdefault(match.group(1), match.group(2))
    task = values.get("Task", "")
    task_kind = _TASK_KIND.search(task)
    if task_kind and not kind:
        kind = task_kind.group(1)
    return ParsedBrief(
        kind=kind,
        title=values.get("Title") or values.get("Summary") or values.get("Subject") or title,
        date=values.get("Date", ""),
        topic=values.get("Topic", ""),
        authors=_names(
            values.get("Authored by")
            or values.get("Reported by")
            or values.get("Prepared by")
            or values.get("Sent by")
            or values.get("Organized by")
            or ""
        ),
        reviewers=_names(values.get("Reviewed by", "")),
        commenter=commenter,
        replying=replying,
        facts=tuple(facts),
    )
