"""KG-QA questions over the fact ledger (:mod:`enterprise_sim.facts`).

Two reasoning families the ledger makes possible:

* ``temporal`` — the value of a fact now, at a past date, and how often it
  changed (the answer is a ``FactVersion`` node; the label is its value);
* ``conflict`` — which documents state a superseded value, a value that was never
  true, or announced a change (the answer is a set of ``Artifact`` nodes).

Worlds without a ledger yield no questions, so their benchmarks are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from enterprise_sim.core.world import World

if TYPE_CHECKING:
    from enterprise_sim.benchmark.generate import _Draft

__all__ = ["fact_drafts"]


def _subject(world: World, fact_id: str) -> str:
    fact = world.get_node(fact_id)
    if fact is None:
        return fact_id
    subject = world.get_node(str(fact.props.get("subject", "")))
    if subject is None:
        return str(fact.props.get("subject", fact_id))
    return str(subject.props.get("name", subject.id))


def fact_drafts(world: World) -> Iterator[_Draft]:
    """Every ledger question for ``world``, in fact-id order."""
    from enterprise_sim.benchmark.generate import _Draft

    versions: dict[str, list[tuple[int, str, str, str]]] = {}
    for node in world.nodes_by_type("FactVersion"):
        versions.setdefault(str(node.props["fact"]), []).append(
            (
                int(node.props["version"]),
                node.id,
                str(node.props["value"]),
                str(node.props["valid_from"]),
            )
        )
    asserts: dict[str, list[tuple[str, str]]] = {}
    for fact in world.nodes_by_type("Fact"):
        for edge in world.in_edges(fact.id, "asserts"):
            asserts.setdefault(fact.id, []).append((str(edge.props.get("stance")), edge.src))
    for fact in sorted(world.nodes_by_type("Fact"), key=lambda n: n.id):
        rows = sorted(versions.get(fact.id, []))
        if not rows:
            continue
        label = str(fact.props.get("label", fact.props.get("attribute", "")))
        subject = _subject(world, fact.id)
        latest = rows[-1]
        yield _Draft(
            reasoning_type="temporal",
            qtype="current",
            question=f"What is the current {label} of {subject}?",
            expected_ids=(latest[1],),
            expected_label=latest[2],
            difficulty="hard" if len(rows) > 1 else "easy",
        )
        if len(rows) > 1:
            first, second = rows[0], rows[1]
            day = datetime.fromisoformat(first[3]).date().isoformat()
            changed = datetime.fromisoformat(second[3]).date().isoformat()
            yield _Draft(
                reasoning_type="temporal",
                qtype="as_of",
                question=(
                    f"What was the {label} of {subject} on {day}, before it changed on {changed}?"
                ),
                expected_ids=(first[1],),
                expected_label=first[2],
                difficulty="hard",
            )
            yield _Draft(
                reasoning_type="temporal",
                qtype="count",
                question=f"How many times did the {label} of {subject} change?",
                expected_ids=tuple(r[1] for r in rows[1:]),
                expected_label=str(len(rows) - 1),
                difficulty="medium",
            )
        by_stance: dict[str, set[str]] = {}
        for stance, artifact in asserts.get(fact.id, []):
            by_stance.setdefault(stance, set()).add(artifact)
        for stance, text in (
            ("stale", f"Which documents state an outdated {label} for {subject}?"),
            ("conflicting", f"Which documents state a {label} for {subject} that was never true?"),
            ("update", f"Which documents announced a change to the {label} of {subject}?"),
        ):
            found = tuple(sorted(by_stance.get(stance, set())))
            if found:
                yield _Draft(
                    reasoning_type="conflict",
                    qtype="which",
                    question=text,
                    expected_ids=found,
                    expected_label=None,
                    difficulty="hard",
                )
