"""The KG-QA benchmark schema: :class:`QAPair` and the :class:`Benchmark` collection.

A :class:`QAPair` is one question plus its gold answer, grounded in the sim's
ground truth: the answer is the set of knowledge-graph node ids (and an optional
human-readable label) that a correct response must resolve to, tagged with the
kind of reasoning the question exercises. The grader (esim-uzc.3) scores agent
answers against ``expected_ids``; the generators (esim-uzc.2+) mint these pairs
from the gold :class:`~enterprise_sim.core.world.World`.

Pairs serialize as one JSON object per line (JSONL): the on-disk benchmark is an
ordinary ``.jsonl`` file, one :class:`QAPair` per row, read and written through
:class:`Benchmark`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The reasoning categories a question can exercise, from a single hop to a tree
# walk. The generators tag each pair so the report (esim-uzc.6) can break scores
# down by reasoning type — the headline result of the benchmark.
REASONING_TYPES: frozenset[str] = frozenset(
    {
        "direct_relation",  # one edge: "who does X report to?"
        "transitive",  # a chain of like edges: "who is X's skip-level manager?"
        "provenance",  # the answer key: "which artifact states this fact?"
        "aggregation",  # a count/filter over many nodes: "how many … ?"
        "goal_tree",  # a goal/sub-goal decomposition walk.
    }
)


@dataclass(frozen=True)
class QAPair:
    """One benchmark question and its gold answer over the knowledge graph.

    Attributes:
        id: Stable, content-derived identifier for the pair.
        question: The natural-language question posed to the agent.
        qtype: The question's surface form / template family (generator-defined,
            e.g. ``who``/``count``/``list``).
        reasoning_type: The kind of reasoning required; one of
            :data:`REASONING_TYPES`.
        expected_ids: The gold answer — the KG node ids a correct response must
            resolve to. Order-independent in scoring, but stored as a tuple so a
            :class:`QAPair` is hashable/immutable.
        expected_label: An optional human-readable rendering of the answer (e.g.
            the canonical name) for reports; ``None`` when not applicable.
        difficulty: A coarse difficulty tag (generator-defined, e.g.
            ``easy``/``medium``/``hard``).
    """

    id: str
    question: str
    qtype: str
    reasoning_type: str
    expected_ids: tuple[str, ...]
    expected_label: str | None = None
    difficulty: str = "medium"

    def __post_init__(self) -> None:
        if self.reasoning_type not in REASONING_TYPES:
            allowed = ", ".join(sorted(REASONING_TYPES))
            raise ValueError(f"reasoning_type {self.reasoning_type!r} is not one of: {allowed}")
        # Normalize expected_ids to a tuple even when constructed from a list, so
        # callers that pass a list still get a hashable, immutable pair.
        if not isinstance(self.expected_ids, tuple):
            object.__setattr__(self, "expected_ids", tuple(self.expected_ids))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (``expected_ids`` as a list)."""
        return {
            "id": self.id,
            "question": self.question,
            "qtype": self.qtype,
            "reasoning_type": self.reasoning_type,
            "expected_ids": list(self.expected_ids),
            "expected_label": self.expected_label,
            "difficulty": self.difficulty,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QAPair:
        """Reconstruct a :class:`QAPair` from :meth:`to_dict` output."""
        return cls(
            id=data["id"],
            question=data["question"],
            qtype=data["qtype"],
            reasoning_type=data["reasoning_type"],
            expected_ids=tuple(data["expected_ids"]),
            expected_label=data.get("expected_label"),
            difficulty=data.get("difficulty", "medium"),
        )

    def to_jsonl(self) -> str:
        """Serialize to a single canonical JSONL line (no trailing newline)."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> QAPair:
        """Parse a :class:`QAPair` from one JSONL line produced by :meth:`to_jsonl`."""
        return cls.from_dict(json.loads(line))


@dataclass
class Benchmark:
    """An ordered collection of :class:`QAPair`\\ s with JSONL persistence.

    The benchmark file is JSONL: one :class:`QAPair` per line, in order. Reading
    and writing round-trip byte-for-byte through :meth:`read_jsonl` /
    :meth:`write_jsonl`.
    """

    pairs: list[QAPair] = field(default_factory=list)

    def __iter__(self) -> Iterator[QAPair]:
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def to_jsonl(self) -> str:
        """Serialize all pairs to JSONL text (one pair per line, trailing newline)."""
        if not self.pairs:
            return ""
        return "".join(f"{pair.to_jsonl()}\n" for pair in self.pairs)

    def write_jsonl(self, path: str | Path) -> None:
        """Write the benchmark to ``path`` as JSONL (one :class:`QAPair` per line)."""
        Path(path).write_text(self.to_jsonl(), encoding="utf-8")

    @classmethod
    def from_jsonl(cls, text: str) -> Benchmark:
        """Parse a :class:`Benchmark` from JSONL text, skipping blank lines."""
        pairs = [QAPair.from_jsonl(line) for line in text.splitlines() if line.strip()]
        return cls(pairs=pairs)

    @classmethod
    def read_jsonl(cls, path: str | Path) -> Benchmark:
        """Read a :class:`Benchmark` from a JSONL file at ``path``."""
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def of(cls, pairs: Iterable[QAPair]) -> Benchmark:
        """Build a :class:`Benchmark` from any iterable of pairs."""
        return cls(pairs=list(pairs))
