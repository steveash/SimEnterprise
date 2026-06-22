"""Tier 3 — evaluators: structural realism + LLM-as-judge (ARCHITECTURE §13).

Where Tier 1 (:mod:`enterprise_sim.authoring.lint`) reads the *authoring objects*
statically and Tier 2 (the test kit) runs a process in isolation, Tier 3 grades the
**output of a run** — the fully-ordered :class:`~enterprise_sim.core.events.EventJournal`
the scheduler emits (§15.4). It answers a different question than the earlier tiers:
not "is this playbook well-formed?" but "does the corpus it produced *look like* a
real enterprise's activity?". A process/playbook is "not done until Tiers 2–3 pass".

Two halves, mirroring §13 Tier 3:

* **Structural realism metrics** (no LLM, deterministic, free) computed straight off
  the journal plus the working calendar:

  - ``comments_per_reviewer`` — review comments should be shared across reviewers,
    not dumped by one. Scored as the *balance* (1 − Gini) of the per-reviewer
    comment-count distribution.
  - ``working_hours_adherence`` — events should land inside working time (I1); the
    fraction that do.
  - ``cadence_plausibility`` — recurring activity should be *spread over the
    timeline*, not collapsed onto one instant; the fraction of recurring event
    types whose occurrences span multiple working days with positive gaps.
  - ``role_participation_balance`` — work should be distributed across the people
    bound to each role, not monopolized; the mean per-role balance (1 − Gini).

* **Optional LLM-as-judge** (:func:`judge_sample`) — samples one artifact-bearing
  event deterministically and asks the §7 provider to rate its *content* realism.
  The fake backend makes this reproducible and network-free in tests (D31).

The public surface mirrors the linter: :class:`Metric` (one graded measure),
:class:`JudgeVerdict` (the judge's call), :class:`EvalReport` (the collected result
with an ``ok`` verdict), and the entry points :func:`evaluate`, :func:`judge_sample`,
and :func:`format_report`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from enterprise_sim.core.config.seed import substream
from enterprise_sim.core.events import Event, EventJournal
from enterprise_sim.core.llm.prompt import assemble_prompt
from enterprise_sim.core.sim.calendar import WorkingCalendar

if TYPE_CHECKING:
    from enterprise_sim.core.llm.client import LLMClient

__all__ = [
    "EvalReport",
    "JudgeVerdict",
    "Metric",
    "Thresholds",
    "evaluate",
    "format_report",
    "judge_sample",
]


# --------------------------------------------------------------------------- #
# Public value types.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Pass thresholds for the structural metrics (tunable per call).

    Each value is the minimum score (``0..1``, higher is more realistic) at which
    the corresponding metric is considered to pass. Defaults are deliberately
    lenient — Tier 3 is a realism *smell test*, not a hard correctness gate — and a
    caller (or CI) can tighten them. See each metric in :func:`evaluate` for what
    its score means.
    """

    comments_per_reviewer: float = 0.4
    working_hours_adherence: float = 0.95
    cadence_plausibility: float = 0.5
    role_participation_balance: float = 0.4


@dataclass(frozen=True, slots=True)
class Metric:
    """One graded realism measure.

    Attributes:
        name: Stable metric key (``"working_hours_adherence"`` …) so callers and
            tests can assert on a measure without parsing prose.
        value: The score, in ``0..1`` where higher is more realistic.
        threshold: The minimum passing score, or ``None`` for an *informational*
            metric — one that never fails the report (e.g. because there was no
            data to grade, such as a journal with no review comments).
        detail: Human-readable one-line summary.
        sample: Structured supporting evidence (per-person counts, violator ids,
            per-type gaps) for drill-down and tests.
    """

    name: str
    value: float
    threshold: float | None
    detail: str
    sample: dict[str, Any] = field(default_factory=dict)

    @property
    def applicable(self) -> bool:
        """``True`` iff this metric carries a pass threshold (was gradable)."""
        return self.threshold is not None

    @property
    def passed(self) -> bool:
        """``True`` iff informational, or the score meets its threshold."""
        return self.threshold is None or self.value >= self.threshold

    def __str__(self) -> str:
        if self.threshold is None:
            verdict = "n/a"
        else:
            verdict = "pass" if self.passed else "FAIL"
        return f"{verdict}: {self.name}={self.value:.3f} ({self.detail})"


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """The LLM-as-judge's call on one sampled artifact (§13 Tier 3).

    Attributes:
        event_id: The journal event whose artifact was judged.
        artifact_kind: The deliverable kind judged (``"design_doc"`` …).
        score: Realism score normalized to ``0..1`` (from the model's 1–5 rating).
        rating: The raw 1–5 rating the model returned.
        rationale: The model's one-line justification.
        model: The model id that produced the verdict.
        cache_hit: Whether the response came from the on-disk cache (D31).
        raw: The full structured payload the judge returned.
    """

    event_id: str
    artifact_kind: str
    score: float
    rating: int
    rationale: str
    model: str
    cache_hit: bool
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        cached = " (cached)" if self.cache_hit else ""
        return (
            f"judge: {self.artifact_kind} [{self.event_id}] "
            f"score={self.score:.2f} (rating {self.rating}/5){cached}: {self.rationale}"
        )


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The collected Tier 3 result: structural metrics plus an optional judge call.

    ``ok`` is ``True`` iff every *applicable* metric passed (informational metrics
    and the judge never fail the report — the judge is advisory content signal, not
    a structural gate).
    """

    metrics: tuple[Metric, ...] = ()
    judge: JudgeVerdict | None = None

    @property
    def ok(self) -> bool:
        """``True`` iff no applicable metric is below its threshold."""
        return all(m.passed for m in self.metrics)

    def metric(self, name: str) -> Metric:
        """Return the metric named ``name``. Raises ``KeyError`` if absent."""
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(name)

    def failures(self) -> tuple[Metric, ...]:
        """The applicable metrics that fell below their threshold, in report order."""
        return tuple(m for m in self.metrics if m.applicable and not m.passed)


# --------------------------------------------------------------------------- #
# Statistics helpers.
# --------------------------------------------------------------------------- #


def _gini(values: Sequence[float]) -> float:
    """Return the Gini coefficient of ``values`` (0 = equal, 1 = maximally unequal).

    Defined for non-negative values; an empty or all-zero input is treated as
    perfectly equal (``0.0``). Used to turn a count distribution into an
    inequality measure whose complement (``1 - gini``) is a balance score.
    """
    xs = [float(v) for v in values if v >= 0]
    n = len(xs)
    total = sum(xs)
    if n == 0 or total == 0.0:
        return 0.0
    diffs = sum(abs(a - b) for a in xs for b in xs)
    return diffs / (2.0 * n * total)


def _balance(counts: Sequence[float]) -> float:
    """Balance score (``1 - gini``) of a count distribution; ``1.0`` for ≤1 group."""
    if len(counts) <= 1:
        return 1.0
    return 1.0 - _gini(counts)


def _is_comment(event: Event, comment_types: frozenset[str] | None) -> bool:
    """Whether ``event`` is a review comment.

    With an explicit ``comment_types`` set, membership decides. Otherwise the
    scheduler's own marker is used: every spread-emitted comment carries an
    ``in_reply_to`` payload key (threaded to a parent), and no structural step
    event does — so its presence identifies a comment unambiguously (§15.2).
    """
    if comment_types is not None:
        return event.type in comment_types
    return "in_reply_to" in event.payload


def _commenters(event: Event) -> list[str]:
    """The person ids credited as authoring ``event`` (flattened across roles)."""
    return [pid for ids in event.actors.values() for pid in ids]


# --------------------------------------------------------------------------- #
# Structural metrics.
# --------------------------------------------------------------------------- #


def _comments_per_reviewer(
    events: Sequence[Event], comment_types: frozenset[str] | None, threshold: float
) -> Metric:
    """Grade the per-reviewer comment-count distribution (balance = 1 − Gini)."""
    counts: Counter[str] = Counter()
    total = 0
    for event in events:
        if not _is_comment(event, comment_types):
            continue
        for person in _commenters(event):
            counts[person] += 1
            total += 1
    if total == 0:
        return Metric(
            "comments_per_reviewer",
            value=1.0,
            threshold=None,
            detail="no review comments in journal",
            sample={"per_reviewer": {}, "total": 0},
        )
    value = _balance(list(counts.values()))
    return Metric(
        "comments_per_reviewer",
        value=value,
        threshold=threshold,
        detail=f"{total} comments across {len(counts)} reviewer(s)",
        sample={"per_reviewer": dict(counts), "total": total},
    )


def _working_hours_adherence(
    events: Sequence[Event], calendar: WorkingCalendar, threshold: float
) -> Metric:
    """Grade the fraction of events landing inside working time (I1)."""
    if not events:
        return Metric(
            "working_hours_adherence",
            value=1.0,
            threshold=None,
            detail="no events in journal",
            sample={"violations": 0, "total": 0},
        )
    violators: list[str] = []
    for event in events:
        if not calendar.is_working(event.timestamp):
            violators.append(event.id)
    adherent = len(events) - len(violators)
    value = adherent / len(events)
    return Metric(
        "working_hours_adherence",
        value=value,
        threshold=threshold,
        detail=f"{adherent}/{len(events)} events in working hours",
        # Cap the recorded sample so a pathological run can't bloat the report.
        sample={"violations": len(violators), "total": len(events), "violators": violators[:20]},
    )


def _cadence_plausibility(
    events: Sequence[Event], min_occurrences: int, threshold: float
) -> Metric:
    """Grade whether recurring event types spread across the timeline.

    A type recurring ``min_occurrences`` times or more is *plausible* when its
    occurrences fall on more than one distinct day (the failure mode this catches
    is every firing collapsing onto a single instant). The score is the fraction
    of recurring types that are plausible; per-type mean gaps (in days) are
    reported for drill-down.
    """
    by_type: dict[str, list[datetime]] = defaultdict(list)
    for event in events:
        by_type[event.type].append(event.timestamp)

    recurring = {t: sorted(ts) for t, ts in by_type.items() if len(ts) >= min_occurrences}
    if not recurring:
        return Metric(
            "cadence_plausibility",
            value=1.0,
            threshold=None,
            detail=f"no event type recurs ≥{min_occurrences} times",
            sample={"recurring_types": {}},
        )

    detail_by_type: dict[str, dict[str, Any]] = {}
    plausible = 0
    for event_type, stamps in recurring.items():
        distinct_days = len({ts.date() for ts in stamps})
        gaps_days = [
            (b - a).total_seconds() / 86400.0 for a, b in zip(stamps, stamps[1:], strict=False)
        ]
        mean_gap = sum(gaps_days) / len(gaps_days) if gaps_days else 0.0
        is_plausible = distinct_days >= 2 and mean_gap > 0.0
        plausible += int(is_plausible)
        detail_by_type[event_type] = {
            "count": len(stamps),
            "distinct_days": distinct_days,
            "mean_gap_days": round(mean_gap, 3),
            "plausible": is_plausible,
        }

    value = plausible / len(recurring)
    return Metric(
        "cadence_plausibility",
        value=value,
        threshold=threshold,
        detail=f"{plausible}/{len(recurring)} recurring type(s) spread over the timeline",
        sample={"recurring_types": detail_by_type},
    )


def _role_participation_balance(events: Sequence[Event], threshold: float) -> Metric:
    """Grade how evenly work is distributed across the people bound to each role."""
    by_role: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        for role, ids in event.actors.items():
            for person in ids:
                by_role[role][person] += 1
    if not by_role:
        return Metric(
            "role_participation_balance",
            value=1.0,
            threshold=None,
            detail="no actor participation in journal",
            sample={"per_role": {}},
        )

    per_role: dict[str, dict[str, Any]] = {}
    balances: list[float] = []
    for role, counts in by_role.items():
        balance = _balance(list(counts.values()))
        balances.append(balance)
        per_role[role] = {
            "balance": round(balance, 3),
            "people": len(counts),
            "counts": dict(counts),
        }

    value = sum(balances) / len(balances)
    return Metric(
        "role_participation_balance",
        value=value,
        threshold=threshold,
        detail=f"mean balance over {len(by_role)} role(s)",
        sample={"per_role": per_role},
    )


def evaluate(
    journal: EventJournal | Iterable[Event],
    *,
    calendar: WorkingCalendar | None = None,
    thresholds: Thresholds | None = None,
    comment_types: Iterable[str] | None = None,
    min_recurring: int = 3,
) -> EvalReport:
    """Compute the structural realism metrics for ``journal`` (Tier 3, no LLM).

    Args:
        journal: The run's event journal (or any iterable of events). Events are
            graded in canonical order when an :class:`EventJournal` is given.
        calendar: Working calendar for the working-hours check; a default
            Mon–Fri 09:00–17:00 calendar is used when omitted (must match the
            calendar the run used for the adherence figure to be meaningful).
        thresholds: Pass thresholds; sensible lenient defaults when omitted.
        comment_types: Explicit event types to count as review comments; when
            omitted, comments are auto-detected by the scheduler's ``in_reply_to``
            marker.
        min_recurring: Minimum occurrences for an event type to count as
            "recurring" in the cadence check.

    Returns:
        An :class:`EvalReport` whose ``ok`` is ``True`` iff every applicable
        metric met its threshold.
    """
    events = journal.ordered() if isinstance(journal, EventJournal) else list(journal)
    calendar = calendar or WorkingCalendar()
    thresholds = thresholds or Thresholds()
    types = frozenset(comment_types) if comment_types is not None else None

    metrics = (
        _comments_per_reviewer(events, types, thresholds.comments_per_reviewer),
        _working_hours_adherence(events, calendar, thresholds.working_hours_adherence),
        _cadence_plausibility(events, min_recurring, thresholds.cadence_plausibility),
        _role_participation_balance(events, thresholds.role_participation_balance),
    )
    return EvalReport(metrics=metrics)


# --------------------------------------------------------------------------- #
# Optional LLM-as-judge (§13 Tier 3, §16.3).
# --------------------------------------------------------------------------- #

#: System prompt for the content-realism judge (cacheable, per §16.1).
_JUDGE_SYSTEM = (
    "You are a meticulous evaluator of enterprise corpus realism. You are shown the "
    "metadata of a single business artifact event drawn from a simulated company's "
    "activity log. Judge how plausible this artifact is as something a real "
    "organization would produce — the right kind of deliverable, sensible "
    "participants and timing, a coherent intent. Rate realism from 1 (clearly "
    "synthetic / implausible) to 5 (indistinguishable from real)."
)

#: Forced-output schema for the judge. ``rating`` is an enum so even the fake
#: backend yields an in-range value, and real models are constrained to 1–5.
_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rating": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "realistic": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["rating", "rationale"],
}


def _artifact_events(
    events: Sequence[Event], deliverable_kinds: frozenset[str] | None
) -> list[Event]:
    """Artifact-bearing events eligible for judging, sorted by id for determinism."""
    chosen = [
        e
        for e in events
        if e.deliverable is not None
        and (deliverable_kinds is None or e.deliverable.kind in deliverable_kinds)
    ]
    return sorted(chosen, key=lambda e: e.id)


def _artifact_brief(event: Event) -> str:
    """Render one artifact event's metadata as the volatile judge brief."""
    assert event.deliverable is not None
    roles = ", ".join(f"{role}={len(ids)}" for role, ids in sorted(event.actors.items()) if ids)
    intent = event.payload.get("intent") or event.payload.get("topic") or "(unspecified)"
    return (
        "Artifact under review:\n"
        f"- event type: {event.type}\n"
        f"- deliverable: {event.deliverable.kind} ({event.deliverable.medium})\n"
        f"- timestamp: {event.timestamp.isoformat()}\n"
        f"- project: {event.project or '(none)'}\n"
        f"- participants by role: {roles or '(none)'}\n"
        f"- intent: {intent}\n"
    )


def judge_sample(
    journal: EventJournal | Iterable[Event],
    client: LLMClient,
    *,
    root_seed: int = 0,
    deliverable_kinds: Iterable[str] | None = None,
) -> JudgeVerdict | None:
    """Sample one artifact-bearing event and have the LLM judge its content realism.

    A single artifact is sampled *deterministically* (a seeded draw over the
    id-sorted candidate set), so the same journal and seed always judge the same
    artifact — the structural-determinism principle (§7) applied to evaluation.
    The judge call goes through the provided :class:`LLMClient`, so a fake-backed
    client makes this reproducible and network-free in tests (D31), while an
    api/bedrock/cli client judges for real.

    Returns:
        A :class:`JudgeVerdict`, or ``None`` when the journal holds no
        artifact-bearing event to sample.
    """
    events = journal.ordered() if isinstance(journal, EventJournal) else list(journal)
    kinds = frozenset(deliverable_kinds) if deliverable_kinds is not None else None
    candidates = _artifact_events(events, kinds)
    if not candidates:
        return None

    rng = substream(root_seed, "eval", "judge")
    event = candidates[rng.randrange(len(candidates))]

    prompt = assemble_prompt(system=_JUDGE_SYSTEM, brief=_artifact_brief(event))
    result = client.generate_structured(prompt, _JUDGE_SCHEMA)
    data = result.data

    rating = int(data.get("rating", 1))
    rating = min(5, max(1, rating))
    return JudgeVerdict(
        event_id=event.id,
        artifact_kind=event.deliverable.kind if event.deliverable is not None else "",
        score=(rating - 1) / 4.0,
        rating=rating,
        rationale=str(data.get("rationale", "")),
        model=result.model,
        cache_hit=result.cache_hit,
        raw=dict(data),
    )


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def format_report(report: EvalReport, target: str) -> str:
    """Render an :class:`EvalReport` as human-readable lines for the CLI."""
    verdict = "ok" if report.ok else "FAILED"
    failures = report.failures()
    header = f"{target}: {verdict} ({len(failures)} metric failure(s))"
    lines = [header]
    lines.extend(f"  {m}" for m in report.metrics)
    if report.judge is not None:
        lines.append(f"  {report.judge}")
    return "\n".join(lines)
