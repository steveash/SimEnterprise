"""Tier 3 evaluator tests (ARCHITECTURE §13).

Acceptance (esim-bd4fe9b3): the structural realism metrics run on a sample (a real
scheduler-produced journal grades clean), each metric flags its own failure mode on
a hand-built journal, and the LLM-as-judge runs deterministically against the fake
backend.
"""

from __future__ import annotations

from datetime import datetime

from enterprise_sim.authoring.eval import (
    EvalReport,
    Metric,
    Thresholds,
    evaluate,
    format_report,
    judge_sample,
)
from enterprise_sim.core.events import Deliverable, Event, EventJournal
from enterprise_sim.core.llm import LLMConfig, build_client
from enterprise_sim.core.sim import (
    Activation,
    Effect,
    OnStart,
    Process,
    Scenario,
    Scheduler,
    Selector,
    Spread,
    Step,
    WorkingCalendar,
)
from enterprise_sim.core.sim.spec import RoleSpec
from enterprise_sim.core.world import Node, World

# A Monday 09:00 anchor and a four-week window (mirrors the scheduler tests).
START = datetime(2026, 1, 5, 9, 0)
END = datetime(2026, 1, 30, 17, 0)
CAL = WorkingCalendar()


# --------------------------------------------------------------------------- #
# Helpers — synthetic events + a real scheduler run.
# --------------------------------------------------------------------------- #


def _evt(
    eid: str,
    etype: str,
    ts: datetime,
    *,
    actors: dict[str, list[str]] | None = None,
    deliverable: Deliverable | None = None,
    payload: dict[str, object] | None = None,
) -> Event:
    return Event(
        id=eid,
        type=etype,
        timestamp=ts,
        actors=actors or {},
        deliverable=deliverable,
        payload=payload or {},
    )


def _world() -> World:
    """A small KG: one author plus a reviewer pool on one project."""
    world = World()
    t0 = datetime(2026, 1, 1, 9, 0)
    world.add_node(Node("person:ada", "Person", t0, props={"team": "eng", "expertise": ["pay"]}))
    for name in ("bob", "cy", "dee", "eve", "fin"):
        world.add_node(
            Node(f"person:{name}", "Person", t0, props={"team": "eng", "expertise": ["pay"]})
        )
    world.add_node(Node("project:checkout", "Project", t0, props={"status": "active"}))
    return world


def _review_scenario() -> Scenario:
    """A draft → spread review → approve process producing an artifact."""
    process = Process(
        name="design_review",
        roles=(
            RoleSpec("author"),
            RoleSpec(
                "reviewers",
                selector=Selector(type="Person", exclude=("person:ada",), count="2..3"),
                relationship="reviews_for",
            ),
        ),
        steps=(
            Step(
                id="draft",
                emits="DeliverableDrafted",
                by="author",
                at="day 0",
                produces=Deliverable(kind="design_doc", medium="document"),
            ),
            Step(
                id="review",
                emits="ReviewOpened",
                by="author",
                after="draft",
                duration="3d",
                spread=Spread(role="reviewers", per_actor="2..4"),
                parent_step="draft",
            ),
            Step(
                id="approve",
                emits="Approved",
                by="author",
                after="review",
                effects=(Effect.mutate("project:checkout", "status", "approved"),),
            ),
        ),
        priority=10,
    )
    return Scenario(
        "review",
        activations=(
            Activation(
                "a_review",
                process,
                OnStart(),
                bind={"author": ("person:ada",)},
                anchor="project:checkout",
            ),
        ),
    )


def _scheduled_journal() -> EventJournal:
    result = Scheduler(_world(), CAL, root_seed=7).run(_review_scenario(), start=START, end=END)
    return result.journal


# --------------------------------------------------------------------------- #
# End-to-end: a real journal grades clean (the "metrics on a sample" acceptance).
# --------------------------------------------------------------------------- #


def test_evaluate_real_journal_is_ok() -> None:
    report = evaluate(_scheduled_journal(), calendar=CAL)
    assert isinstance(report, EvalReport)
    assert report.ok, [str(m) for m in report.failures()]
    # All four structural metrics are present.
    assert {m.name for m in report.metrics} == {
        "comments_per_reviewer",
        "working_hours_adherence",
        "cadence_plausibility",
        "role_participation_balance",
    }


def test_scheduled_journal_all_in_working_hours() -> None:
    report = evaluate(_scheduled_journal(), calendar=CAL)
    adherence = report.metric("working_hours_adherence")
    assert adherence.value == 1.0
    assert adherence.sample["violations"] == 0


def test_scheduled_journal_has_balanced_reviewer_comments() -> None:
    report = evaluate(_scheduled_journal(), calendar=CAL)
    comments = report.metric("comments_per_reviewer")
    assert comments.sample["total"] > 0
    assert comments.applicable
    assert comments.passed


# --------------------------------------------------------------------------- #
# Per-metric failure fixtures.
# --------------------------------------------------------------------------- #


def test_working_hours_adherence_flags_off_hours_events() -> None:
    journal = EventJournal(
        [
            _evt("e1", "Posted", datetime(2026, 1, 5, 10, 0)),  # Mon 10:00 — working
            _evt("e2", "Posted", datetime(2026, 1, 5, 22, 0)),  # Mon 22:00 — after hours
            _evt("e3", "Posted", datetime(2026, 1, 4, 12, 0)),  # Sun noon — weekend
        ]
    )
    report = evaluate(journal, calendar=CAL)
    metric = report.metric("working_hours_adherence")
    assert metric.value == 1 / 3
    assert metric.sample["violations"] == 2
    assert set(metric.sample["violators"]) == {"e2", "e3"}
    assert not metric.passed
    assert not report.ok


def test_comments_per_reviewer_flags_monopolized_review() -> None:
    # One reviewer posts 20 comments; four others post 1 each — highly unbalanced.
    events = [_evt("draft", "Drafted", datetime(2026, 1, 5, 9, 0))]
    for n in range(20):
        events.append(
            _evt(
                f"c-bob-{n}",
                "CommentPosted",
                datetime(2026, 1, 5, 10, 0),
                actors={"reviewers": ["person:bob"]},
                payload={"in_reply_to": "draft"},
            )
        )
    for who in ("cy", "dee", "eve", "fin"):
        events.append(
            _evt(
                f"c-{who}-0",
                "CommentPosted",
                datetime(2026, 1, 5, 11, 0),
                actors={"reviewers": [f"person:{who}"]},
                payload={"in_reply_to": "draft"},
            )
        )
    report = evaluate(EventJournal(events), calendar=CAL)
    metric = report.metric("comments_per_reviewer")
    assert metric.sample["total"] == 24
    assert metric.sample["per_reviewer"]["person:bob"] == 20
    assert not metric.passed


def test_comments_per_reviewer_not_applicable_without_comments() -> None:
    journal = EventJournal([_evt("e1", "Posted", datetime(2026, 1, 5, 10, 0))])
    metric = evaluate(journal, calendar=CAL).metric("comments_per_reviewer")
    assert not metric.applicable
    assert metric.threshold is None
    assert metric.passed  # informational metrics never fail the report


def test_comments_per_reviewer_honors_explicit_types() -> None:
    events = [
        _evt(
            "x1",
            "Note",
            datetime(2026, 1, 5, 10, 0),
            actors={"reviewers": ["person:bob"]},
        ),
        _evt(
            "x2",
            "Note",
            datetime(2026, 1, 5, 11, 0),
            actors={"reviewers": ["person:cy"]},
        ),
    ]
    metric = evaluate(EventJournal(events), calendar=CAL, comment_types=["Note"]).metric(
        "comments_per_reviewer"
    )
    assert metric.sample["total"] == 2
    assert metric.passed  # perfectly balanced (one each)


def test_cadence_plausibility_flags_collapsed_timeline() -> None:
    # Five firings of one type all at the same instant — no temporal spread.
    instant = datetime(2026, 1, 5, 9, 0)
    journal = EventJournal([_evt(f"s{n}", "Standup", instant) for n in range(5)])
    metric = evaluate(journal, calendar=CAL).metric("cadence_plausibility")
    assert metric.value == 0.0
    assert metric.sample["recurring_types"]["Standup"]["distinct_days"] == 1
    assert not metric.passed


def test_cadence_plausibility_passes_for_spread_cadence() -> None:
    # A weekly standup across four distinct days.
    days = [datetime(2026, 1, 5 + 7 * n, 9, 0) for n in range(4)]
    journal = EventJournal([_evt(f"s{n}", "Standup", d) for n, d in enumerate(days)])
    metric = evaluate(journal, calendar=CAL).metric("cadence_plausibility")
    assert metric.value == 1.0
    assert metric.passed
    assert metric.sample["recurring_types"]["Standup"]["mean_gap_days"] == 7.0


def test_cadence_not_applicable_below_min_recurring() -> None:
    journal = EventJournal(
        [
            _evt("a", "Alpha", datetime(2026, 1, 5, 9, 0)),
            _evt("b", "Beta", datetime(2026, 1, 6, 9, 0)),
        ]
    )
    metric = evaluate(journal, calendar=CAL).metric("cadence_plausibility")
    assert not metric.applicable
    assert metric.passed


def test_role_participation_balance_flags_monopoly() -> None:
    # One author does 20 drafts; four teammates do one each — unbalanced "author" role.
    events = [
        _evt(f"d{n}", "Drafted", datetime(2026, 1, 5, 9, 0), actors={"author": ["person:ada"]})
        for n in range(20)
    ]
    for i, who in enumerate(("bob", "cy", "dee", "eve")):
        events.append(
            _evt(
                f"x{i}", "Drafted", datetime(2026, 1, 6, 9, 0), actors={"author": [f"person:{who}"]}
            )
        )
    metric = evaluate(EventJournal(events), calendar=CAL).metric("role_participation_balance")
    assert metric.sample["per_role"]["author"]["people"] == 5
    assert not metric.passed


def test_role_participation_balance_passes_when_even() -> None:
    events = [
        _evt("d1", "Drafted", datetime(2026, 1, 5, 9, 0), actors={"author": ["person:ada"]}),
        _evt("d2", "Drafted", datetime(2026, 1, 6, 9, 0), actors={"author": ["person:bob"]}),
    ]
    metric = evaluate(EventJournal(events), calendar=CAL).metric("role_participation_balance")
    assert metric.value == 1.0
    assert metric.passed


# --------------------------------------------------------------------------- #
# Thresholds + report rendering.
# --------------------------------------------------------------------------- #


def test_custom_thresholds_can_tighten_adherence() -> None:
    journal = EventJournal(
        [
            _evt("e1", "Posted", datetime(2026, 1, 5, 10, 0)),
            _evt("e2", "Posted", datetime(2026, 1, 5, 22, 0)),
        ]
    )
    lenient = evaluate(journal, calendar=CAL, thresholds=Thresholds(working_hours_adherence=0.4))
    assert lenient.metric("working_hours_adherence").passed
    strict = evaluate(journal, calendar=CAL, thresholds=Thresholds(working_hours_adherence=0.9))
    assert not strict.metric("working_hours_adherence").passed


def test_format_report_lists_metrics_and_verdict() -> None:
    report = evaluate(_scheduled_journal(), calendar=CAL)
    text = format_report(report, "run-xyz")
    assert text.startswith("run-xyz: ok")
    assert "working_hours_adherence" in text
    assert "cadence_plausibility" in text


def test_metric_str_renders_verdict() -> None:
    m = Metric("demo", value=0.5, threshold=0.9, detail="d")
    assert str(m).startswith("FAIL: demo=0.500")
    info = Metric("demo", value=0.5, threshold=None, detail="d")
    assert str(info).startswith("n/a:")


# --------------------------------------------------------------------------- #
# LLM-as-judge against the fake backend (deterministic, network-free).
# --------------------------------------------------------------------------- #


def test_judge_sample_runs_against_fake_backend() -> None:
    client = build_client(LLMConfig(backend="fake"))
    verdict = judge_sample(_scheduled_journal(), client, root_seed=7)
    assert verdict is not None
    assert verdict.artifact_kind == "design_doc"
    assert 1 <= verdict.rating <= 5
    assert 0.0 <= verdict.score <= 1.0
    assert verdict.model  # the fake backend reports a model id


def test_judge_sample_is_deterministic() -> None:
    journal = _scheduled_journal()
    a = judge_sample(journal, build_client(LLMConfig(backend="fake")), root_seed=7)
    b = judge_sample(journal, build_client(LLMConfig(backend="fake")), root_seed=7)
    assert a is not None and b is not None
    assert (a.event_id, a.rating, a.rationale) == (b.event_id, b.rating, b.rationale)


def test_judge_sample_returns_none_without_artifacts() -> None:
    journal = EventJournal([_evt("e1", "Posted", datetime(2026, 1, 5, 10, 0))])
    client = build_client(LLMConfig(backend="fake"))
    assert judge_sample(journal, client) is None


def test_judge_sample_respects_deliverable_kind_filter() -> None:
    journal = _scheduled_journal()
    client = build_client(LLMConfig(backend="fake"))
    assert judge_sample(journal, client, deliverable_kinds=["nonexistent_kind"]) is None
    kept = judge_sample(journal, client, deliverable_kinds=["design_doc"], root_seed=7)
    assert kept is not None and kept.artifact_kind == "design_doc"
