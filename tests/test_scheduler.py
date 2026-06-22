"""Tests for the discrete-event scheduler (ARCHITECTURE §15, D26/D27).

Covers the acceptance criteria of esim-8ec97524: a trivial process runs
deterministically; the busy map stays non-overlapping; comment threads are
well-formed; and all six triggers — including effect-driven ``OnCondition`` and
the ``OnEvent`` cascade — fire as specified.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from enterprise_sim.core.sim import (
    Activation,
    BusyMap,
    Condition,
    Effect,
    OnCadence,
    OnCondition,
    OnEvent,
    OnMilestone,
    OnStart,
    Probabilistic,
    Process,
    Scenario,
    Scheduler,
    Selector,
    Spread,
    Step,
    WorkingCalendar,
)
from enterprise_sim.core.sim.scheduler import cadence_firings, probabilistic_firings
from enterprise_sim.core.sim.spec import (
    EventPredicate,
    RoleSpec,
    parse_business_days,
    parse_duration_hours,
    parse_int_range,
)
from enterprise_sim.core.world import Node, World

# A Monday 09:00 and a four-week window for placement headroom.
START = datetime(2026, 1, 5, 9, 0)
END = datetime(2026, 1, 30, 17, 0)
CAL = WorkingCalendar()


def _world() -> World:
    """A small KG: one author plus a reviewer pool, on one project."""
    world = World()
    t0 = datetime(2026, 1, 1, 9, 0)
    world.add_node(Node("person:ada", "Person", t0, props={"team": "eng", "expertise": ["pay"]}))
    for name in ("bob", "cy", "dee", "eve", "fin"):
        world.add_node(
            Node(f"person:{name}", "Person", t0, props={"team": "eng", "expertise": ["pay"]})
        )
    world.add_node(Node("project:checkout", "Project", t0, props={"status": "active"}))
    return world


def _author_role() -> RoleSpec:
    return RoleSpec("author")


# --------------------------------------------------------------------------- #
# Parsers.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [("day 0", 0), ("day 3", 3), ("5", 5), ("  day 2 ", 2)],
)
def test_parse_business_days(text: str, expected: int) -> None:
    assert parse_business_days(text) == expected


def test_parse_business_days_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        parse_business_days("day -1")


@pytest.mark.parametrize(
    ("text", "hpd", "expected"),
    [("2h", 8.0, 2.0), ("1d", 8.0, 8.0), ("1.5d", 8.0, 12.0), ("3", 8.0, 3.0)],
)
def test_parse_duration_hours(text: str, hpd: float, expected: float) -> None:
    assert parse_duration_hours(text, hpd) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [(3, (3, 3)), ("2..5", (2, 5)), ("4", (4, 4))],
)
def test_parse_int_range(spec: int | str, expected: tuple[int, int]) -> None:
    assert parse_int_range(spec) == expected


def test_parse_int_range_rejects_inverted() -> None:
    with pytest.raises(ValueError, match="invalid count range"):
        parse_int_range("5..2")


# --------------------------------------------------------------------------- #
# Cadence + probabilistic firing computation.
# --------------------------------------------------------------------------- #


def test_cadence_weekly_lands_on_named_weekday() -> None:
    fires = cadence_firings("weekly:WED", START, END, CAL)
    assert fires == [
        datetime(2026, 1, 7, 9, 0),
        datetime(2026, 1, 14, 9, 0),
        datetime(2026, 1, 21, 9, 0),
        datetime(2026, 1, 28, 9, 0),
    ]
    assert all(f.weekday() == 2 for f in fires)


def test_cadence_daily_workdays_skips_weekends() -> None:
    fires = cadence_firings("daily:workdays", START, datetime(2026, 1, 11, 17, 0), CAL)
    # Mon..Fri of the first week only (Jan 10/11 are the weekend).
    assert [f.day for f in fires] == [5, 6, 7, 8, 9]
    assert all(f.weekday() in CAL.working_weekdays for f in fires)


def test_cadence_every_interval_steps_and_snaps_to_working_time() -> None:
    fires = cadence_firings("every:2w", START, END, CAL)
    assert fires[0] == datetime(2026, 1, 5, 9, 0)
    assert fires[1] == datetime(2026, 1, 19, 9, 0)
    assert all(CAL.is_working(f) for f in fires)


def test_cadence_per_sprint_defaults_to_two_weeks() -> None:
    assert cadence_firings("per_sprint:2w", START, END, CAL) == cadence_firings(
        "every:2w", START, END, CAL
    )


def test_cadence_unknown_rule_raises() -> None:
    with pytest.raises(ValueError, match="unknown cadence rule"):
        cadence_firings("hourly:5", START, END, CAL)


def test_probabilistic_firings_are_seeded_and_in_window() -> None:
    import random

    fires_a = probabilistic_firings(
        Probabilistic(rate=2.0, per="week"), START, END, CAL, random.Random(7)
    )
    fires_b = probabilistic_firings(
        Probabilistic(rate=2.0, per="week"), START, END, CAL, random.Random(7)
    )
    assert fires_a == fires_b
    assert fires_a  # some arrivals over four weeks at ~2/week
    assert all(START <= f <= END for f in fires_a)
    assert all(CAL.is_working(f) for f in fires_a)


def test_probabilistic_zero_rate_never_fires() -> None:
    import random

    assert probabilistic_firings(Probabilistic(rate=0.0), START, END, CAL, random.Random(1)) == []


# --------------------------------------------------------------------------- #
# Busy map.
# --------------------------------------------------------------------------- #


def test_busy_map_books_non_overlapping_slots() -> None:
    from datetime import timedelta

    busy = BusyMap(CAL)
    s1, f1 = busy.book("p", START, timedelta(hours=2), kind="m", source_event="e1")
    s2, f2 = busy.book("p", START, timedelta(hours=2), kind="m", source_event="e2")
    assert not f1 and not f2
    assert s1 == START
    assert s2 >= s1 + timedelta(hours=2)
    assert not BusyMap.has_overlap(busy.intervals("p"))


def test_busy_map_flags_forced_overlap_for_oversized_slot() -> None:
    from datetime import timedelta

    busy = BusyMap(CAL)
    # A 20-working-hour slot cannot fit in one 8h day → forced.
    _, forced = busy.book("p", START, timedelta(hours=20), kind="m", source_event="e")
    assert forced


# --------------------------------------------------------------------------- #
# Condition / event predicate evaluation.
# --------------------------------------------------------------------------- #


def test_condition_evaluates_against_world() -> None:
    world = _world()
    cond = Condition("project:checkout", "status", "eq", "approved")
    assert cond.evaluate(world) is False
    world.get_node("project:checkout").props["status"] = "approved"  # type: ignore[union-attr]
    assert cond.evaluate(world) is True
    assert cond.watched_attrs() == frozenset({"status"})


def test_event_predicate_matches_flattened_fields() -> None:
    pred = EventPredicate("payload.kind", "eq", "design")
    assert pred.matches({"payload.kind": "design"}) is True
    assert pred.matches({"payload.kind": "status"}) is False
    assert pred.matches({}) is False


# --------------------------------------------------------------------------- #
# Scheduler — acceptance criteria.
# --------------------------------------------------------------------------- #


def _trivial_scenario() -> Scenario:
    proc = Process(
        name="weekly_status",
        roles=(_author_role(),),
        steps=(Step(id="post", emits="StatusPosted", by="author"),),
        priority=10,
    )
    return Scenario(
        "trivial",
        activations=(
            Activation(
                "a",
                proc,
                OnCadence("weekly:MON"),
                bind={"author": ("person:ada",)},
                anchor="project:checkout",
            ),
        ),
    )


def test_trivial_process_runs_deterministically() -> None:
    scenario = _trivial_scenario()

    def once() -> str:
        result = Scheduler(_world(), CAL, root_seed=99).run(scenario, start=START, end=END)
        return result.journal.dumps()

    first, second = once(), once()
    assert first == second
    assert first.count("StatusPosted") == 4  # four Mondays in the window


def test_ordered_log_is_monotonic() -> None:
    result = Scheduler(_world(), CAL, root_seed=1).run(_full_scenario(), start=START, end=END)
    ordered = result.journal.ordered()
    assert all(a.timestamp <= b.timestamp for a, b in zip(ordered, ordered[1:], strict=False))


def test_comment_threads_are_well_formed() -> None:
    result = Scheduler(_world(), CAL, root_seed=5).run(_review_scenario(), start=START, end=END)
    ids = {e.id for e in result.journal}
    comments = [e for e in result.journal if e.type == "CommentPosted"]
    assert comments  # the spread actually produced comments
    for comment in comments:
        # Every reply resolves to an already-existing event (I4).
        assert comment.parent_event in ids
    # No comment threads to an event that comes after it in time.
    by_id = {e.id: e for e in result.journal}
    for comment in comments:
        parent = by_id[comment.parent_event]
        assert parent.timestamp <= comment.timestamp


def test_busy_map_has_no_overlaps() -> None:
    result = Scheduler(_world(), CAL, root_seed=5).run(_review_scenario(), start=START, end=END)
    for person in result.busy_map.people():
        assert not BusyMap.has_overlap(result.busy_map.intervals(person)), person


def test_per_person_calendars_derive_from_busy_map() -> None:
    result = Scheduler(_world(), CAL, root_seed=5).run(_review_scenario(), start=START, end=END)
    cal_nodes = result.world.nodes_by_type("CalendarEvent")
    bookings = result.busy_map.all_bookings()
    assert len(cal_nodes) == len(bookings)
    # Each calendar node links back to its person.
    for node in cal_nodes:
        person = node.props["person"]
        edges = result.world.out_edges(person, "has_calendar_event")
        assert any(e.dst == node.id for e in edges)


def test_review_with_resolver_and_spread_is_deterministic() -> None:
    # The strong I6 guarantee: resolver draws + comment counts + comment placement
    # all reproduce, so two seeded runs emit a byte-identical log and identical edges.
    def once() -> tuple[str, str]:
        result = Scheduler(_world(), CAL, root_seed=17).run(
            _review_scenario(), start=START, end=END
        )
        edges = "\n".join(sorted(e.id for e in result.world.edges_by_type("reviews_for")))
        return result.journal.dumps(), edges

    assert once() == once()


# -- the six triggers ------------------------------------------------------- #


def _review_process() -> Process:
    return Process(
        name="design_review",
        roles=(
            _author_role(),
            RoleSpec(
                "reviewers",
                selector=Selector(type="Person", exclude=("person:ada",), count="2..3"),
                relationship="reviews_for",
            ),
        ),
        steps=(
            Step(id="draft", emits="DeliverableDrafted", by="author", at="day 0"),
            Step(
                id="review",
                emits="ReviewOpened",
                by="author",
                after="draft",
                duration="3d",
                spread=Spread(role="reviewers", per_actor="1..3"),
                parent_step="draft",
            ),
            Step(
                id="approve",
                emits="Approved",
                by="author",
                after="review",
                effects=(
                    Effect.milestone("design_approved"),
                    Effect.mutate("project:checkout", "status", "approved"),
                ),
            ),
        ),
        priority=10,
    )


def _review_scenario() -> Scenario:
    return Scenario(
        "review",
        activations=(
            Activation(
                "a_review",
                _review_process(),
                OnStart(),
                bind={"author": ("person:ada",)},
                anchor="project:checkout",
            ),
        ),
    )


def _full_scenario() -> Scenario:
    review = _review_process()
    ship = Process(
        name="ship",
        roles=(_author_role(),),
        steps=(Step(id="ship", emits="Shipped", by="author"),),
        priority=20,
    )
    notify = Process(
        name="notify",
        roles=(_author_role(),),
        steps=(Step(id="notify", emits="Notified", by="author"),),
        priority=30,
    )
    react = Process(
        name="react",
        roles=(_author_role(),),
        steps=(Step(id="react", emits="Reacted", by="author"),),
        priority=40,
    )
    return Scenario(
        "full",
        activations=(
            Activation(
                "a_start",
                review,
                OnStart(),
                bind={"author": ("person:ada",)},
                anchor="project:checkout",
            ),
            Activation(
                "a_cadence",
                review,
                OnCadence("weekly:WED"),
                bind={"author": ("person:bob",)},
                anchor="project:checkout",
            ),
            Activation(
                "a_milestone",
                ship,
                OnMilestone("design_approved"),
                bind={"author": ("person:ada",)},
                anchor="project:checkout",
            ),
            Activation(
                "a_condition",
                notify,
                OnCondition(Condition("project:checkout", "status", "eq", "approved")),
                bind={"author": ("person:cy",)},
                anchor="project:checkout",
            ),
            Activation(
                "a_event",
                react,
                OnEvent("Approved"),
                bind={"author": ("person:dee",)},
                anchor="project:checkout",
            ),
            Activation(
                "a_prob",
                ship,
                Probabilistic(rate=1.0, per="week"),
                bind={"author": ("person:eve",)},
                anchor="project:checkout",
            ),
        ),
    )


def test_all_six_triggers_fire() -> None:
    result = Scheduler(_world(), CAL, root_seed=3).run(_full_scenario(), start=START, end=END)
    counts: dict[str, int] = {}
    for event in result.journal:
        counts[event.type] = counts.get(event.type, 0) + 1

    # OnStart + OnCadence both instantiate the review process.
    assert counts.get("DeliverableDrafted", 0) >= 2
    # OnEvent: Approved triggers Reacted (cascade); within-window firings only.
    assert 1 <= counts.get("Reacted", 0) <= counts.get("Approved", 0)
    # OnMilestone: each design_approved milestone ships.
    assert counts.get("Shipped", 0) >= 1
    # OnCondition: status→approved notifies exactly once (fire-once gate).
    assert counts.get("Notified", 0) == 1
    # Probabilistic contributes additional Shipped beyond the milestone ones.
    assert counts.get("Shipped", 0) >= counts.get("Approved", 0)


def test_on_event_cascade_is_reactive() -> None:
    # A pure reactive chain: start → Alpha, B reacts to Alpha → Beta, C reacts to Beta → Gamma.
    a = Process(
        "A", roles=(_author_role(),), steps=(Step(id="s", emits="Alpha", by="author"),), priority=10
    )
    b = Process(
        "B", roles=(_author_role(),), steps=(Step(id="s", emits="Beta", by="author"),), priority=20
    )
    c = Process(
        "C", roles=(_author_role(),), steps=(Step(id="s", emits="Gamma", by="author"),), priority=30
    )
    scenario = Scenario(
        "cascade",
        activations=(
            Activation("a", a, OnStart(), bind={"author": ("person:ada",)}),
            Activation("b", b, OnEvent("Alpha"), bind={"author": ("person:bob",)}),
            Activation("c", c, OnEvent("Beta"), bind={"author": ("person:cy",)}),
        ),
    )
    result = Scheduler(_world(), CAL, root_seed=1).run(scenario, start=START, end=END)
    types = [e.type for e in result.journal.ordered()]
    assert types.count("Alpha") == 1
    assert types.count("Beta") == 1
    assert types.count("Gamma") == 1
    # Each link is strictly after the one that triggered it.
    order = {t: i for i, t in enumerate(types)}
    assert order["Alpha"] < order["Beta"] < order["Gamma"]


def test_on_condition_effect_driven_fires_without_waiting_for_tick() -> None:
    # The status flips at draft time; the notify must fire off the effect, and the
    # condition is a gate (fires once) even though a daily tick also runs.
    flip = Process(
        "flip",
        roles=(_author_role(),),
        steps=(
            Step(
                id="flip",
                emits="Flipped",
                by="author",
                effects=(Effect.mutate("project:checkout", "status", "approved"),),
            ),
        ),
        priority=10,
    )
    notify = Process(
        "notify",
        roles=(_author_role(),),
        steps=(Step(id="n", emits="Notified", by="author"),),
        priority=20,
    )
    scenario = Scenario(
        "cond",
        activations=(
            Activation(
                "a", flip, OnStart(), bind={"author": ("person:ada",)}, anchor="project:checkout"
            ),
            Activation(
                "b",
                notify,
                OnCondition(Condition("project:checkout", "status", "eq", "approved")),
                bind={"author": ("person:bob",)},
            ),
        ),
    )
    result = Scheduler(_world(), CAL, root_seed=1).run(scenario, start=START, end=END)
    notified = [e for e in result.journal if e.type == "Notified"]
    assert len(notified) == 1


def test_max_events_cap_stops_runaway_cycle() -> None:
    # A self-triggering cycle: the process reacts to its own event type.
    loop = Process(
        "loop",
        roles=(_author_role(),),
        steps=(Step(id="s", emits="Tick", by="author"),),
        priority=10,
    )
    scenario = Scenario(
        "loop",
        activations=(
            Activation("seed", loop, OnStart(), bind={"author": ("person:ada",)}),
            Activation("react", loop, OnEvent("Tick"), bind={"author": ("person:ada",)}),
        ),
    )
    result = Scheduler(_world(), CAL, root_seed=1).run(
        scenario, start=START, end=END, max_events=50
    )
    assert any(issue.code == "max_events" for issue in result.issues)
    assert len(result.journal) <= 60  # cap + the final overshoot, then it stops


def test_resolver_writes_relationship_edges() -> None:
    result = Scheduler(_world(), CAL, root_seed=5).run(_review_scenario(), start=START, end=END)
    reviews_for = result.world.edges_by_type("reviews_for")
    assert reviews_for  # reviewers were bound and edges written
    assert all(e.dst == "project:checkout" for e in reviews_for)


def test_end_before_start_raises() -> None:
    with pytest.raises(ValueError, match="precedes start"):
        Scheduler(_world(), CAL, root_seed=1).run(_trivial_scenario(), start=END, end=START)
