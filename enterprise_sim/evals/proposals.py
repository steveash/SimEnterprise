"""Validate & id-assign AI-proposed eval questions (EXPLORER_EVALS.md §1/§4, ``evals add``).

A proposal is a raw JSON object (from the eval-proposal chat, or a hand-written
file passed to ``evals add --file``) shaped like a
:class:`~enterprise_sim.benchmark.schema.QAPair` minus ``id`` and ``source``:
``{"question", "reasoning_type", "expected_ids", "expected_label"?, "difficulty"?,
"qtype"?, "tags"?}``. :func:`validate_proposals` never raises on a bad
proposal — it is rejected (with a reason) and validation continues — so one
malformed entry in a batch never aborts the rest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enterprise_sim.benchmark.schema import REASONING_TYPES, QAPair
from enterprise_sim.evals.store import read_questions

# Mirrors enterprise_sim.benchmark.generate._qid exactly, so a proposal that
# happens to restate a *generated* question collides on id (and is rejected as
# a duplicate) rather than being appended as a second, redundant pair.
_SEP = "\x00"


def qid(reasoning_type: str, qtype: str, question: str, expected_ids: tuple[str, ...]) -> str:
    """The content-hash id scheme shared with the deterministic generators."""
    digest = hashlib.sha1(usedforsecurity=False)
    for part in (reasoning_type, qtype, question, *sorted(expected_ids)):
        digest.update(part.encode("utf-8"))
        digest.update(_SEP.encode("utf-8"))
    return f"qa-{digest.hexdigest()[:12]}"


@dataclass(frozen=True, slots=True)
class Rejection:
    """One proposal that failed validation, with a human-readable reason."""

    question: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"question": self.question, "reason": self.reason}


def load_node_ids(run_dir: str | Path) -> frozenset[str]:
    """Every node id in ``<run>/kg/nodes.jsonl`` — what ``expected_ids`` must be a subset of."""
    path = Path(run_dir) / "kg" / "nodes.jsonl"
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(json.loads(line)["id"])
    return frozenset(ids)


def validate_proposals(
    run_dir: str | Path,
    raw_proposals: Iterable[dict[str, Any]],
    *,
    node_ids: frozenset[str] | None = None,
) -> tuple[list[QAPair], list[Rejection]]:
    """Validate raw proposal dicts against the run's gold KG; return ``(accepted, rejected)``.

    A proposal is rejected when: a required field (``question``,
    ``reasoning_type``, ``expected_ids``) is missing or empty;
    ``reasoning_type`` is not one of :data:`REASONING_TYPES`; an
    ``expected_ids`` entry is not a real node id in
    ``<run>/kg/nodes.jsonl``; or its content-hash id (:func:`qid`) duplicates a
    question already in the run's set or earlier in this same batch. Accepted
    pairs get ``source="proposed"``.
    """
    ids = node_ids if node_ids is not None else load_node_ids(run_dir)
    existing_ids = {p.id for p in read_questions(run_dir)}
    accepted: list[QAPair] = []
    rejected: list[Rejection] = []
    seen_in_batch: set[str] = set()

    for raw in raw_proposals:
        question = str(raw.get("question") or "").strip()
        reasoning_type = str(raw.get("reasoning_type") or "")
        raw_expected = raw.get("expected_ids")
        expected_ids = tuple(raw_expected) if raw_expected else ()
        qtype = str(raw.get("qtype") or "what")
        expected_label = raw.get("expected_label")
        difficulty = str(raw.get("difficulty") or "medium")
        tags = tuple(raw.get("tags") or ())

        if not question:
            rejected.append(Rejection(question, "empty question"))
            continue
        if reasoning_type not in REASONING_TYPES:
            rejected.append(
                Rejection(
                    question,
                    f"reasoning_type {reasoning_type!r} is not one of {sorted(REASONING_TYPES)}",
                )
            )
            continue
        if not expected_ids:
            rejected.append(Rejection(question, "expected_ids is empty"))
            continue
        unknown = sorted(i for i in expected_ids if i not in ids)
        if unknown:
            rejected.append(
                Rejection(question, f"expected_ids not found in kg/nodes.jsonl: {unknown}")
            )
            continue

        pair_id = qid(reasoning_type, qtype, question, expected_ids)
        if pair_id in existing_ids or pair_id in seen_in_batch:
            rejected.append(Rejection(question, "duplicate of an existing question"))
            continue
        seen_in_batch.add(pair_id)
        accepted.append(
            QAPair(
                id=pair_id,
                question=question,
                qtype=qtype,
                reasoning_type=reasoning_type,
                expected_ids=expected_ids,
                expected_label=str(expected_label) if expected_label is not None else None,
                difficulty=difficulty,
                source="proposed",
                tags=tags,
            )
        )
    return accepted, rejected


__all__ = ["Rejection", "load_node_ids", "qid", "validate_proposals"]
