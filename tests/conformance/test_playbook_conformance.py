"""The built-in playbook conformance suite P1–P6 (ARCHITECTURE §13, esim-bb00bb20).

The P-suite judges the triggering graph statically (covering ``impl`` processes via
their ``declares`` block). Two of the three reference playbooks are P-clean; the third
(``build_software``) carries a known dead OnMilestone trigger (esim-xk7) that P2
correctly surfaces — encoded here as a detection test, not a silent pass.
"""

from __future__ import annotations

from enterprise_sim.authoring import sdk
from enterprise_sim.authoring import testkit as tk
from enterprise_sim.authoring.patterns import build_software, run_clinical_study, sell_merchandise


def _codes(violations: list[tk.ConformanceViolation]) -> set[str]:
    return {v.code for v in violations}


# --------------------------------------------------------------------------- #
# Reference playbooks.
# --------------------------------------------------------------------------- #


def test_sell_merchandise_is_p_clean() -> None:
    assert tk.check_playbook(sell_merchandise()) == []


def test_run_clinical_study_is_p_clean() -> None:
    assert tk.check_playbook(run_clinical_study()) == []


def test_build_software_p2_catches_dead_milestone() -> None:
    # esim-xk7: retro_on_ship waits on a milestone nothing emits → unreachable.
    violations = tk.check_playbook(build_software())
    assert "P2" in _codes(violations)
    assert any(v.subject == "retro_on_ship" for v in violations if v.code == "P2")


# --------------------------------------------------------------------------- #
# P1 — dead OnEvent trigger.
# --------------------------------------------------------------------------- #


def test_p1_flags_onevent_with_no_emitter() -> None:
    proc = sdk.Process(
        name="reactor",
        roles=(sdk.Role(name="a"),),
        steps=(sdk.Step(id="s", by="a", at="day 0", emits=(sdk.EmittedEvent("Out"),)),),
        declares=sdk.Declares(events=("Out",)),
    )
    pb = sdk.Playbook(
        name="dead_trigger",
        vertical="test",
        activations=(sdk.Activation(id="x", process=proc, trigger=sdk.OnEvent("NobodyEmitsThis")),),
    )
    assert "P1" in _codes(tk.check_playbook(pb))


def test_p1_clean_when_emitter_exists() -> None:
    emitter = sdk.Process(
        name="emitter",
        roles=(sdk.Role(name="a"),),
        steps=(sdk.Step(id="s", by="a", at="day 0", emits=(sdk.EmittedEvent("Signal"),)),),
        declares=sdk.Declares(events=("Signal",)),
    )
    reactor = sdk.Process(
        name="reactor",
        roles=(sdk.Role(name="a"),),
        steps=(sdk.Step(id="s", by="a", at="day 0", emits=(sdk.EmittedEvent("Done"),)),),
        declares=sdk.Declares(events=("Done",)),
    )
    pb = sdk.Playbook(
        name="ok",
        vertical="test",
        activations=(
            sdk.Activation(id="e", process=emitter, trigger=sdk.OnStart()),
            sdk.Activation(id="r", process=reactor, trigger=sdk.OnEvent("Signal")),
        ),
    )
    assert "P1" not in _codes(tk.check_playbook(pb))


# --------------------------------------------------------------------------- #
# P3 — unguarded cycle.
# --------------------------------------------------------------------------- #


def test_p3_flags_unguarded_event_cycle() -> None:
    # A emits Ping (OnStart), B reacts to Ping emitting Pong, A also reacts to Pong → cycle.
    a = sdk.Process(
        name="a",
        roles=(sdk.Role(name="x"),),
        steps=(sdk.Step(id="s", by="x", at="day 0", emits=(sdk.EmittedEvent("Ping"),)),),
        declares=sdk.Declares(events=("Ping",)),
    )
    b = sdk.Process(
        name="b",
        roles=(sdk.Role(name="x"),),
        steps=(sdk.Step(id="s", by="x", at="day 0", emits=(sdk.EmittedEvent("Pong"),)),),
        declares=sdk.Declares(events=("Pong",)),
    )
    pb = sdk.Playbook(
        name="loop",
        vertical="test",
        activations=(
            sdk.Activation(id="a_on_pong", process=a, trigger=sdk.OnEvent("Pong")),
            sdk.Activation(id="b_on_ping", process=b, trigger=sdk.OnEvent("Ping")),
        ),
    )
    assert "P3" in _codes(tk.check_playbook(pb))


# --------------------------------------------------------------------------- #
# P4 — uncovered deliverable expectation.
# --------------------------------------------------------------------------- #


def test_p4_flags_unproduced_expectation() -> None:
    proc = sdk.Process(
        name="p",
        roles=(sdk.Role(name="a"),),
        steps=(
            sdk.Step(
                id="s",
                by="a",
                at="day 0",
                emits=(sdk.EmittedEvent("E"),),
                produces=sdk.Deliverable("report", "document"),
            ),
        ),
        declares=sdk.Declares(events=("E",), deliverables=("report",)),
    )
    pb = sdk.Playbook(
        name="gap",
        vertical="test",
        activations=(sdk.Activation(id="x", process=proc, trigger=sdk.OnStart()),),
        deliverable_expectations=("report", "never_made"),
    )
    violations = tk.check_playbook(pb)
    assert "P4" in _codes(violations)
    assert any("never_made" in v.message for v in violations if v.code == "P4")


# --------------------------------------------------------------------------- #
# P5 — infeasible staffing.
# --------------------------------------------------------------------------- #


def test_p5_flags_contradictory_selector() -> None:
    # A selector with mutually exclusive filters can never bind anyone.
    proc = sdk.Process(
        name="impossible",
        roles=(
            sdk.Role(
                name="ghost",
                select=sdk.Selector(
                    type="Person",
                    where=(
                        sdk.Match("team", "eq", "alpha"),
                        sdk.Match("team", "eq", "beta"),
                    ),
                    count=1,
                ),
            ),
        ),
        steps=(sdk.Step(id="s", by="ghost", at="day 0", emits=(sdk.EmittedEvent("E"),)),),
        declares=sdk.Declares(events=("E",)),
    )
    pb = sdk.Playbook(
        name="understaffed",
        vertical="test",
        activations=(sdk.Activation(id="x", process=proc, trigger=sdk.OnStart()),),
    )
    assert "P5" in _codes(tk.check_playbook(pb))


# --------------------------------------------------------------------------- #
# P6 — volume bound.
# --------------------------------------------------------------------------- #


def test_p6_flags_explosive_probabilistic_rate() -> None:
    proc = sdk.Process(
        name="spammer",
        roles=(sdk.Role(name="a"),),
        steps=(sdk.Step(id="s", by="a", at="day 0", emits=(sdk.EmittedEvent("Noise"),)),),
        declares=sdk.Declares(events=("Noise",)),
    )
    pb = sdk.Playbook(
        name="explosive",
        vertical="test",
        activations=(
            sdk.Activation(id="x", process=proc, trigger=sdk.Probabilistic(rate=500.0, per="day")),
        ),
    )
    assert "P6" in _codes(tk.check_playbook(pb))


def test_p6_clean_for_reasonable_cadence() -> None:
    assert "P6" not in _codes(tk.check_playbook(run_clinical_study()))
