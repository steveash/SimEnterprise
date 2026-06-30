"""Score predicted answer sets against the gold benchmark (esim-uzc.3).

Grading is set-based and deterministic: a benchmark answer is the *set* of
knowledge-graph node ids a correct response must resolve to (``expected_ids``),
and an agent's answer is likewise a set (``predicted_ids``). Order and duplicates
never matter — only set membership. There is no LLM here; the same inputs always
yield the same :class:`Report`.

For each :class:`~enterprise_sim.benchmark.schema.QAPair` the grader computes
an :class:`ItemScore`: an exact-match flag plus precision / recall / F1 over the
two id sets. It then aggregates those into a macro-averaged :class:`Aggregate`
overall and one per ``reasoning_type`` — the headline breakdown the report
(esim-uzc.6) renders. Predictions arrive as one JSON object per line (JSONL),
mirroring the benchmark file; a benchmark pair with no matching prediction is
graded against the empty set (the agent declined to answer).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enterprise_sim.benchmark.schema import Benchmark, QAPair

# --------------------------------------------------------------------------- #
# Predictions: an agent's answers, one row per question (JSONL).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Prediction:
    """One agent answer: the node ids predicted for a single question.

    Attributes:
        qa_id: The :class:`~enterprise_sim.benchmark.schema.QAPair` id this answers.
        predicted_ids: The KG node ids the agent resolved to. Order-independent in
            scoring, but stored as a tuple so the row is hashable/immutable.
    """

    qa_id: str
    predicted_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        # Accept a list (e.g. straight from JSON) but keep the row immutable.
        if not isinstance(self.predicted_ids, tuple):
            object.__setattr__(self, "predicted_ids", tuple(self.predicted_ids))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (``predicted_ids`` as a list)."""
        return {"qa_id": self.qa_id, "predicted_ids": list(self.predicted_ids)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Prediction:
        """Reconstruct a :class:`Prediction` from :meth:`to_dict` output."""
        return cls(qa_id=data["qa_id"], predicted_ids=tuple(data["predicted_ids"]))

    def to_jsonl(self) -> str:
        """Serialize to a single canonical JSONL line (no trailing newline)."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> Prediction:
        """Parse a :class:`Prediction` from one JSONL line produced by :meth:`to_jsonl`."""
        return cls.from_dict(json.loads(line))


@dataclass
class Predictions:
    """An agent's answer set, keyed by ``qa_id`` with JSONL persistence.

    The predictions file is JSONL: one :class:`Prediction` per line. Lookups go
    through :meth:`ids_for`, which returns the predicted id *set* for a question
    (the empty set when the agent never answered it).
    """

    by_id: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.by_id)

    def __iter__(self) -> Iterator[Prediction]:
        return (Prediction(qa_id, ids) for qa_id, ids in self.by_id.items())

    def ids_for(self, qa_id: str) -> frozenset[str]:
        """The predicted id set for ``qa_id`` (empty when unanswered)."""
        return frozenset(self.by_id.get(qa_id, ()))

    @classmethod
    def of(cls, predictions: Iterable[Prediction]) -> Predictions:
        """Build from any iterable of :class:`Prediction`\\ s (last row wins on dupes)."""
        return cls(by_id={p.qa_id: p.predicted_ids for p in predictions})

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Iterable[str]]) -> Predictions:
        """Build directly from a ``qa_id -> ids`` mapping."""
        return cls(by_id={qa_id: tuple(ids) for qa_id, ids in mapping.items()})

    @classmethod
    def from_jsonl(cls, text: str) -> Predictions:
        """Parse from JSONL text, skipping blank lines (last row wins on dupes)."""
        rows = (Prediction.from_jsonl(line) for line in text.splitlines() if line.strip())
        return cls.of(rows)

    @classmethod
    def read_jsonl(cls, path: str | Path) -> Predictions:
        """Read predictions from a JSONL file at ``path``."""
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))

    def to_jsonl(self) -> str:
        """Serialize all predictions to JSONL text (one row per line, trailing newline)."""
        if not self.by_id:
            return ""
        return "".join(f"{p.to_jsonl()}\n" for p in self)

    def write_jsonl(self, path: str | Path) -> None:
        """Write the predictions to ``path`` as JSONL (one row per line)."""
        Path(path).write_text(self.to_jsonl(), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Set-based precision / recall / F1.
# --------------------------------------------------------------------------- #


def _prf(expected: frozenset[str], predicted: frozenset[str]) -> tuple[float, float, float]:
    """Set precision, recall, F1 for one item.

    Precision is undefined with no prediction and recall is undefined with no gold
    answer; both degenerate cases resolve to ``1.0`` only when the *other* set is
    also empty (a correct "nothing" answer) and ``0.0`` otherwise. F1 is the
    harmonic mean, or ``0.0`` when precision and recall are both zero.
    """
    true_positives = len(expected & predicted)
    precision = true_positives / len(predicted) if predicted else (1.0 if not expected else 0.0)
    recall = true_positives / len(expected) if expected else (1.0 if not predicted else 0.0)
    denom = precision + recall
    f1 = 2.0 * precision * recall / denom if denom else 0.0
    return precision, recall, f1


@dataclass(frozen=True, slots=True)
class ItemScore:
    """The grade for one question: an exact-match flag plus P/R/F1 over id sets."""

    qa_id: str
    reasoning_type: str
    expected: tuple[str, ...]
    predicted: tuple[str, ...]
    exact_match: bool
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True, slots=True)
class Aggregate:
    """Macro-averaged scores over a group of :class:`ItemScore`\\ s.

    Macro-averaging weights every question equally (the unweighted mean of the
    per-item scores), so a reasoning type with few questions still counts. An
    empty group scores ``0.0`` across the board with ``count == 0``.
    """

    count: int
    exact_match_rate: float
    macro_precision: float
    macro_recall: float
    macro_f1: float

    @classmethod
    def over(cls, items: Iterable[ItemScore]) -> Aggregate:
        """Macro-average ``items`` into an :class:`Aggregate`."""
        scored = list(items)
        n = len(scored)
        if n == 0:
            return cls(0, 0.0, 0.0, 0.0, 0.0)
        return cls(
            count=n,
            exact_match_rate=sum(1 for it in scored if it.exact_match) / n,
            macro_precision=sum(it.precision for it in scored) / n,
            macro_recall=sum(it.recall for it in scored) / n,
            macro_f1=sum(it.f1 for it in scored) / n,
        )


@dataclass(frozen=True, slots=True)
class Report:
    """The full grading result: per-item scores plus macro aggregates."""

    items: tuple[ItemScore, ...]
    overall: Aggregate
    by_reasoning_type: dict[str, Aggregate]


def score_item(pair: QAPair, predicted: frozenset[str]) -> ItemScore:
    """Grade one :class:`QAPair` against a predicted id set."""
    expected = frozenset(pair.expected_ids)
    precision, recall, f1 = _prf(expected, predicted)
    return ItemScore(
        qa_id=pair.id,
        reasoning_type=pair.reasoning_type,
        expected=tuple(sorted(expected)),
        predicted=tuple(sorted(predicted)),
        exact_match=expected == predicted,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def score(benchmark: Benchmark, predictions: Predictions) -> Report:
    """Grade ``predictions`` against ``benchmark`` and return a :class:`Report`.

    Iterates the benchmark in order — the benchmark, not the predictions, defines
    the question set — grading each pair against its prediction (the empty set
    when unanswered) and macro-averaging the per-item scores overall and per
    ``reasoning_type``. Predictions for ids not in the benchmark are ignored.
    """
    items = tuple(score_item(pair, predictions.ids_for(pair.id)) for pair in benchmark)

    grouped: dict[str, list[ItemScore]] = defaultdict(list)
    for item in items:
        grouped[item.reasoning_type].append(item)
    by_reasoning_type = {
        reasoning: Aggregate.over(group) for reasoning, group in sorted(grouped.items())
    }

    return Report(
        items=items,
        overall=Aggregate.over(items),
        by_reasoning_type=by_reasoning_type,
    )


# --------------------------------------------------------------------------- #
# Human-readable rendering (CLI).
# --------------------------------------------------------------------------- #


def _format_aggregate(label: str, agg: Aggregate) -> str:
    """One aligned summary line for an :class:`Aggregate`."""
    return (
        f"  {label:<16} n={agg.count:<4} "
        f"F1={agg.macro_f1:.3f}  P={agg.macro_precision:.3f}  "
        f"R={agg.macro_recall:.3f}  EM={agg.exact_match_rate:.3f}"
    )


def format_report(report: Report) -> str:
    """Render a :class:`Report` as human-readable lines for the CLI."""
    lines = ["KG-QA benchmark score", _format_aggregate("overall", report.overall)]
    if report.by_reasoning_type:
        lines.append("by reasoning_type:")
        lines.extend(
            _format_aggregate(reasoning, agg) for reasoning, agg in report.by_reasoning_type.items()
        )
    return "\n".join(lines)
