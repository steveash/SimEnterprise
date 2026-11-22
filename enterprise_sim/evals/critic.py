"""The eval critic: how far can each generated question's expected answer be trusted?

The question generators (:mod:`enterprise_sim.benchmark.generate`) derive every
expected answer mechanically from the gold graph, which makes them *consistent*
but not always *reasonable*: a subject name can be shared by two nodes (so the
question is ambiguous), a "who" question can resolve to a team, an answer set
can be so large nobody could list it, or two questions can read identically
while expecting different answers. The critic reviews each pair and records a
``confidence`` in ``[0, 1]`` plus the reasons (``critique``) on the
:class:`~enterprise_sim.benchmark.schema.QAPair`:

* :func:`structural_critique` — keyless graph checks, always run. Each finding
  multiplies the confidence by a penalty, so several small doubts compound.
* :func:`llm_critique` — optional: a model reviews batches of pairs against the
  graph facts around each question's subject and returns a verdict
  (``ok``/``suspect``/``wrong``), its own confidence, and a reason. The final
  confidence is the product of the two; a ``wrong`` verdict caps it at 0.2.

Human-verified pairs (``verified=True``) are never re-critiqued: a person's
answer outranks both critics, and their confidence stays ``1.0``.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise_sim.benchmark.schema import QAPair
from enterprise_sim.core.llm import LLMClient
from enterprise_sim.core.llm.prompt import assemble_prompt
from enterprise_sim.core.llm.types import is_interruption
from enterprise_sim.core.world import Node, World

__all__ = [
    "CritiqueResult",
    "apply_critique",
    "critique_pairs",
    "llm_critique",
    "structural_critique",
]

#: Answer sets larger than these are hard to state (and to score) exactly.
_LARGE_ANSWER = 20
_HUGE_ANSWER = 50

#: A question's opening words -> the node types a sensible answer has (anchored at the
#: start, so a clause like "…through its subgoals" never retypes the question).
_EXPECTED_TYPES: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"^who\b", re.I), frozenset({"Person"})),
    (re.compile(r"^how many people\b", re.I), frozenset({"Person"})),
    (re.compile(r"^(what|which) artifacts\b", re.I), frozenset({"Artifact"})),
    (re.compile(r"^what are the subgoals\b", re.I), frozenset({"Goal"})),
    (re.compile(r"^what goals\b", re.I), frozenset({"Goal"})),
    (re.compile(r"^how many teams\b", re.I), frozenset({"Team"})),
    (re.compile(r"^what team or department\b", re.I), frozenset({"Team", "Department"})),
)


@dataclass(frozen=True, slots=True)
class CritiqueResult:
    """One pair's review: the confidence, the reasons, and which critic produced it."""

    confidence: float
    notes: tuple[str, ...]
    critic: str

    @property
    def critique(self) -> str:
        return "; ".join(self.notes) if self.notes else "no issues found"


def _label(node: Node) -> str:
    """The same human label the generators embed in question text."""
    if node.aliases:
        return node.aliases[0]
    for key in ("name", "title", "statement"):
        value = node.props.get(key)
        if value:
            return str(value)
    return node.id


def _label_index(world: World) -> dict[str, list[Node]]:
    index: dict[str, list[Node]] = defaultdict(list)
    for node in world.nodes():
        index[_label(node)].append(node)
    return index


def _subjects(question: str, labels: dict[str, list[Node]]) -> dict[str, list[Node]]:
    """Labels (≥ 3 chars) that appear verbatim in the question, longest first, non-overlapping."""
    found: dict[str, list[Node]] = {}
    covered: list[tuple[int, int]] = []
    for label in sorted(labels, key=len, reverse=True):
        if len(label) < 3:
            continue
        start = question.find(label)
        if start < 0:
            continue
        end = start + len(label)
        if any(start < e and end > s for s, e in covered):
            continue
        covered.append((start, end))
        found[label] = labels[label]
    return found


def structural_critique(
    pair: QAPair,
    world: World,
    *,
    labels: dict[str, list[Node]] | None = None,
    duplicates: dict[str, set[tuple[str, ...]]] | None = None,
) -> CritiqueResult:
    """Keyless checks of ``pair``'s expected answer against the gold ``world``."""
    labels = labels if labels is not None else _label_index(world)
    notes: list[str] = []
    confidence = 1.0

    answer = [world.get_node(i) for i in pair.expected_ids]
    missing = [i for i, n in zip(pair.expected_ids, answer, strict=True) if n is None]
    if not pair.expected_ids:
        return CritiqueResult(0.0, ("the expected answer is empty",), "structural")
    if missing:
        return CritiqueResult(
            0.0,
            (f"expected answer references {len(missing)} node(s) not in the graph: {missing[:3]}",),
            "structural",
        )
    nodes = [n for n in answer if n is not None]

    for label, matches in _subjects(pair.question, labels).items():
        if len(matches) > 1:
            kinds = ", ".join(sorted({m.type for m in matches}))
            notes.append(f"'{label}' names {len(matches)} different nodes ({kinds}) — ambiguous")
            confidence *= 0.5

    for pattern, allowed in _EXPECTED_TYPES:
        if pattern.search(pair.question):
            off = sorted({n.type for n in nodes} - allowed)
            if off:
                notes.append(
                    f"the question asks for {'/'.join(sorted(allowed))} but the answer includes "
                    f"{', '.join(off)}"
                )
                confidence *= 0.6
            break

    size = len(pair.expected_ids)
    if size > _HUGE_ANSWER:
        notes.append(f"the answer has {size} items — very hard to state or score exactly")
        confidence *= 0.5
    elif size > _LARGE_ANSWER:
        notes.append(f"the answer has {size} items — hard to state exactly")
        confidence *= 0.75

    if pair.reasoning_type == "aggregation" and pair.expected_label not in (None, str(size)):
        notes.append(
            f"the count label says {pair.expected_label} but the answer set has {size} members"
        )
        confidence *= 0.4

    if duplicates is not None:
        variants = duplicates.get(pair.question, set())
        if len(variants) > 1:
            notes.append(
                f"the same question text appears {len(variants)} times with different answers"
            )
            confidence *= 0.4

    return CritiqueResult(round(confidence, 3), tuple(notes), "structural")


_LLM_SYSTEM = """\
You review question/answer pairs for a knowledge-graph QA benchmark about a
synthetic company. Each expected answer was derived mechanically from the graph.
Judge whether a careful human, reading the question, would accept the expected
answer as correct and complete — given the graph facts shown. Flag questions
that are ambiguous, answers of the wrong kind (e.g. a team for "who"), answers
that look incomplete or over-inclusive relative to the question's wording, and
questions whose wording does not match what the answer measures.

Return one review per pair, echoing its id exactly:
  verdict: "ok" (accept), "suspect" (plausible but doubtful), "wrong" (reject)
  confidence: 0.0-1.0, how confident you are the expected answer is right
  reason: one short sentence."""

_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["ok", "suspect", "wrong"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "confidence", "reason"],
            },
        }
    },
    "required": ["reviews"],
}

_MAX_ANSWER_SHOWN = 25
_MAX_FACTS = 30


def _describe(node: Node) -> str:
    return f"{_label(node)} [{node.type}]"


def _brief(pair: QAPair, world: World, labels: dict[str, list[Node]]) -> str:
    answer = [n for n in (world.get_node(i) for i in pair.expected_ids) if n is not None]
    shown = ", ".join(_describe(n) for n in answer[:_MAX_ANSWER_SHOWN])
    more = (
        f" … (+{len(answer) - _MAX_ANSWER_SHOWN} more)" if len(answer) > _MAX_ANSWER_SHOWN else ""
    )
    subjects = [n for nodes in _subjects(pair.question, labels).values() for n in nodes]
    facts: list[str] = []
    for node in subjects:
        for edge in [*world.out_edges(node.id), *world.in_edges(node.id)]:
            src, dst = world.get_node(edge.src), world.get_node(edge.dst)
            if src is None or dst is None:
                continue
            facts.append(f"{_describe(src)} --{edge.type}--> {_describe(dst)}")
            if len(facts) >= _MAX_FACTS:
                break
    fact_text = "\n    ".join(facts) if facts else "(no subject found in the question)"
    return (
        f"- id: {pair.id}\n  reasoning type: {pair.reasoning_type}\n"
        f"  question: {pair.question}\n"
        f"  expected answer ({len(answer)}): {shown}{more}\n"
        f"  graph facts about the question's subject:\n    {fact_text}"
    )


def llm_critique(
    pairs: Sequence[QAPair],
    world: World,
    client: LLMClient,
    *,
    batch_size: int = 8,
    labels: dict[str, list[Node]] | None = None,
) -> dict[str, tuple[str, float, str]]:
    """Ask the model to review ``pairs``; returns ``{id: (verdict, confidence, reason)}``.

    Pairs the model does not echo back (or a batch that errors) are simply absent
    from the result — the caller keeps their structural review.
    """
    labels = labels if labels is not None else _label_index(world)
    reviews: dict[str, tuple[str, float, str]] = {}
    wanted = {p.id for p in pairs}
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        brief = "Review these pairs:\n\n" + "\n\n".join(_brief(p, world, labels) for p in batch)
        prompt = assemble_prompt(system=_LLM_SYSTEM, brief=brief)
        try:
            data = client.generate_structured(prompt, _LLM_SCHEMA).data
        except Exception as exc:  # noqa: BLE001 - a failed batch leaves those pairs structural-only
            if is_interruption(exc):
                raise
            continue
        for item in data.get("reviews", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or item.get("id") not in wanted:
                continue
            verdict = str(item.get("verdict", "suspect"))
            if verdict not in ("ok", "suspect", "wrong"):
                verdict = "suspect"
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            reviews[str(item["id"])] = (
                verdict,
                min(1.0, max(0.0, confidence)),
                str(item.get("reason", "")).strip(),
            )
    return reviews


def apply_critique(pair: QAPair, result: CritiqueResult) -> QAPair:
    """``pair`` with its review fields set from ``result`` (human-verified pairs are untouched)."""
    if pair.verified:
        return pair
    return dataclasses.replace(
        pair, confidence=result.confidence, critique=result.critique, critic=result.critic
    )


def critique_pairs(
    pairs: Iterable[QAPair], world: World, *, client: LLMClient | None = None
) -> list[QAPair]:
    """Review every pair (structurally, plus the model when ``client`` is given)."""
    pairs = list(pairs)
    labels = _label_index(world)
    duplicates: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for pair in pairs:
        duplicates[pair.question].add(tuple(sorted(pair.expected_ids)))

    structural = {
        p.id: structural_critique(p, world, labels=labels, duplicates=duplicates) for p in pairs
    }
    reviews: dict[str, tuple[str, float, str]] = {}
    if client is not None:
        reviews = llm_critique([p for p in pairs if not p.verified], world, client, labels=labels)

    out: list[QAPair] = []
    for pair in pairs:
        result = structural[pair.id]
        if pair.id in reviews:
            verdict, llm_conf, reason = reviews[pair.id]
            confidence = result.confidence * llm_conf
            if verdict == "wrong":
                confidence = min(confidence, 0.2)
            note = f"model review ({verdict}): {reason}" if reason else f"model review: {verdict}"
            result = CritiqueResult(round(confidence, 3), (*result.notes, note), "llm")
        out.append(apply_critique(pair, result))
    return out
