"""Tier 1 static linter tests (ARCHITECTURE §13, D23).

Acceptance (esim-85acfff5): the linter lints a *good* playbook clean, and a
fixture for *each defect class* is flagged. The first test group asserts the
three reference playbooks lint clean; the rest each construct a minimal playbook
or process exhibiting one defect class and assert its code is reported with the
right severity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from enterprise_sim.authoring.lint import (
    Severity,
    lint_playbook,
    lint_process,
    scan_impl_source,
)
from enterprise_sim.authoring.patterns import REFERENCE_PLAYBOOKS
from enterprise_sim.authoring.sdk import (
    Activation,
    ConditionExpr,
    Declares,
    Deliverable,
    EmittedEvent,
    KGEffect,
    Match,
    OnCadence,
    OnCondition,
    OnEvent,
    OnMilestone,
    OnStart,
    Playbook,
    Probabilistic,
    Process,
    Role,
    Selector,
    Spread,
    Step,
)

# --------------------------------------------------------------------------- #
# Helpers — minimal well-formed building blocks to perturb one at a time.
# --------------------------------------------------------------------------- #


def _role(name: str = "author") -> Role:
    return Role(name=name, select=Selector(type="Person", count=1))


def _emit_process(name: str = "p", event: str = "Done") -> Process:
    """A minimal, internally consistent declarative process."""
    return Process(
        name=name,
        roles=(_role(),),
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent(event),)),),
        declares=Declares(events=(event,)),
    )


def _playbook(*activations: Activation, name: str = "pb") -> Playbook:
    return Playbook(name=name, vertical="technology", activations=activations)


# --------------------------------------------------------------------------- #
# 0. The good playbook lints clean.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(REFERENCE_PLAYBOOKS))
def test_reference_playbooks_lint_clean(name: str) -> None:
    result = lint_playbook(REFERENCE_PLAYBOOKS[name]())
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.diagnostics == (), [str(d) for d in result.diagnostics]


def test_lint_process_on_reference_process_is_clean() -> None:
    pb = REFERENCE_PLAYBOOKS["build_software"]()
    proc = next(a.process for a in pb.activations if a.process.name == "design_review")
    assert lint_process(proc).ok


# --------------------------------------------------------------------------- #
# 1. Type / schema defects.
# --------------------------------------------------------------------------- #


def test_bad_timing_at_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(Step(id="go", by="author", at="someday", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    result = lint_process(proc)
    assert "bad-timing" in result.codes()
    assert not result.ok


def test_bad_duration_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(Step(id="go", by="author", duration="3weeks", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "bad-timing" in lint_process(proc).codes()


def test_conflicting_at_and_after_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(
            Step(id="a", by="author", at="day 0", emits=(EmittedEvent("E"),)),
            Step(id="b", by="author", at="day 1", after="a", emits=(EmittedEvent("F"),)),
        ),
        declares=Declares(events=("E", "F")),
    )
    assert "conflicting-timing" in lint_process(proc).codes()


def test_bad_count_range_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(Role(name="author", select=Selector(type="Person", count="5..2")),),
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "bad-count" in lint_process(proc).codes()


def test_bad_per_actor_range_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(), Role(name="crew", select=Selector(type="Person", count=3))),
        steps=(
            Step(
                id="go",
                by="author",
                at="day 0",
                duration="2d",
                emits=(EmittedEvent("E"),),
                repeat=Spread(role="crew", per_actor="notanumber"),
            ),
        ),
        declares=Declares(events=("E", "CommentPosted")),
    )
    assert "bad-count" in lint_process(proc).codes()


def test_bad_cadence_rule_is_flagged() -> None:
    pb = _playbook(
        Activation(id="a", process=_emit_process(), trigger=OnCadence("fortnightly:BLERG")),
    )
    assert "bad-cadence" in lint_playbook(pb).codes()


def test_bad_probabilistic_rate_is_flagged() -> None:
    pb = _playbook(
        Activation(id="a", process=_emit_process(), trigger=Probabilistic(rate=0.0)),
    )
    assert "bad-rate" in lint_playbook(pb).codes()


def test_bad_condition_operator_is_flagged() -> None:
    bad = ConditionExpr(node="sku:x", attr="stock", op="approx", value=1)  # type: ignore[arg-type]
    pb = _playbook(Activation(id="a", process=_emit_process(), trigger=OnCondition(bad)))
    assert "bad-condition" in lint_playbook(pb).codes()


def test_bad_match_operator_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(
            Role(
                name="author",
                select=Selector(type="Person", where=(Match("team", "like", "eng"),)),  # type: ignore[arg-type]
            ),
        ),
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "bad-operator" in lint_process(proc).codes()


def test_bad_rank_signal_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(Role(name="author", select=Selector(type="Person", rank_by=("charisma",))),),
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "bad-rank-signal" in lint_process(proc).codes()


# --------------------------------------------------------------------------- #
# 2. Reference integrity defects.
# --------------------------------------------------------------------------- #


def test_unknown_role_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(Step(id="go", by="ghost", at="day 0", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "unknown-role" in lint_process(proc).codes()


def test_bad_after_target_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(Step(id="go", by="author", after="nope", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "bad-after" in lint_process(proc).codes()


def test_bad_parent_step_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(
            Step(id="go", by="author", at="day 0", parent_step="nope", emits=(EmittedEvent("E"),)),
        ),
        declares=Declares(events=("E",)),
    )
    assert "bad-parent" in lint_process(proc).codes()


def test_step_cycle_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(
            Step(id="a", by="author", after="b", emits=(EmittedEvent("E"),)),
            Step(id="b", by="author", after="a", emits=(EmittedEvent("F"),)),
        ),
        declares=Declares(events=("E", "F")),
    )
    assert "step-cycle" in lint_process(proc).codes()


def test_duplicate_step_id_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(
            Step(id="go", by="author", at="day 0", emits=(EmittedEvent("E"),)),
            Step(id="go", by="author", at="day 1", emits=(EmittedEvent("F"),)),
        ),
        declares=Declares(events=("E", "F")),
    )
    assert "duplicate-step" in lint_process(proc).codes()


def test_duplicate_activation_id_is_flagged() -> None:
    pb = _playbook(
        Activation(id="x", process=_emit_process("p1"), trigger=OnStart()),
        Activation(id="x", process=_emit_process("p2"), trigger=OnStart()),
    )
    assert "duplicate-activation" in lint_playbook(pb).codes()


def test_impl_and_steps_is_flagged() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        impl="enterprise_sim.processes.thing:Thing",
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E",)),
    )
    assert "impl-and-steps" in lint_process(proc).codes()


def test_empty_process_is_warned() -> None:
    proc = Process(name="p", roles=(_role(),), declares=Declares())
    result = lint_process(proc)
    assert "empty-process" in result.codes()
    # A warning alone does not fail the lint.
    assert result.ok


# --------------------------------------------------------------------------- #
# 3. declares conformance defects.
# --------------------------------------------------------------------------- #


def test_under_declared_event_is_error() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent("Secret"),)),),
        declares=Declares(events=()),
    )
    result = lint_process(proc)
    assert "under-declared-event" in result.codes()
    assert not result.ok


def test_under_declared_deliverable_is_error() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(
            Step(
                id="go",
                by="author",
                at="day 0",
                emits=(EmittedEvent("E"),),
                produces=Deliverable("report", "document"),
            ),
        ),
        declares=Declares(events=("E",)),
    )
    assert "under-declared-deliverable" in lint_process(proc).codes()


def test_under_declared_effect_is_error() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(
            Step(
                id="go",
                by="author",
                at="day 0",
                emits=(EmittedEvent("E"),),
                effects=(KGEffect.milestone("shipped"),),
            ),
        ),
        declares=Declares(events=("E",)),
    )
    assert "under-declared-effect" in lint_process(proc).codes()


def test_over_declared_event_is_warning() -> None:
    proc = Process(
        name="p",
        roles=(_role(),),
        steps=(Step(id="go", by="author", at="day 0", emits=(EmittedEvent("E"),)),),
        declares=Declares(events=("E", "NeverEmitted")),
    )
    result = lint_process(proc)
    assert "over-declared-event" in result.codes()
    assert result.ok  # over-declaration is a warning only


def test_impl_process_declares_is_trusted() -> None:
    # An impl process declares events with no steps to check against — no conformance
    # error (conformance I5 checks it dynamically at Tier 2).
    proc = Process(
        name="p",
        roles=(_role(),),
        impl="enterprise_sim.processes.missing:Missing",
        declares=Declares(events=("Anything",), deliverables=("doc",), effects=("mutate:x",)),
    )
    codes = lint_process(proc).codes()
    assert "under-declared-event" not in codes
    assert "over-declared-event" not in codes


# --------------------------------------------------------------------------- #
# 4. Event-graph soundness defects.
# --------------------------------------------------------------------------- #


def test_dead_trigger_is_flagged() -> None:
    pb = _playbook(
        Activation(id="root", process=_emit_process("p", "Started"), trigger=OnStart()),
        Activation(id="react", process=_emit_process("q", "QDone"), trigger=OnEvent("NobodyEmits")),
    )
    result = lint_playbook(pb)
    assert "dead-trigger" in result.codes()
    assert not result.ok


def test_unreachable_process_is_flagged() -> None:
    # 'react' waits on an event 'Mid' that *is* emitted — but only by 'orphan',
    # which itself is reachable from nothing, so 'react' is unreachable too.
    orphan = _emit_process("orphan", "Mid")
    react = _emit_process("react", "End")
    pb = _playbook(
        Activation(id="orphan", process=orphan, trigger=OnEvent("NeverFired")),
        Activation(id="react", process=react, trigger=OnEvent("Mid")),
    )
    result = lint_playbook(pb)
    assert "unreachable-process" in result.codes()


def test_external_milestone_trigger_is_reachable() -> None:
    # An OnMilestone whose milestone no process emits is treated as an external
    # project-lifecycle root, so it is NOT reported unreachable.
    pb = _playbook(
        Activation(id="root", process=_emit_process("p", "Started"), trigger=OnStart()),
        Activation(id="ship", process=_emit_process("q", "Retro"), trigger=OnMilestone("released")),
    )
    result = lint_playbook(pb)
    assert "unreachable-process" not in result.codes()


def test_unguarded_cycle_is_flagged() -> None:
    # A emits 'Ping' -> triggers B; B emits 'Pong' -> triggers A. Neither guarded.
    a = Process(
        name="a",
        roles=(_role(),),
        steps=(Step(id="s", by="author", at="day 0", emits=(EmittedEvent("Ping"),)),),
        declares=Declares(events=("Ping",)),
    )
    b = Process(
        name="b",
        roles=(_role(),),
        steps=(Step(id="s", by="author", at="day 0", emits=(EmittedEvent("Pong"),)),),
        declares=Declares(events=("Pong",)),
    )
    pb = _playbook(
        Activation(id="A", process=a, trigger=OnEvent("Pong")),
        Activation(id="B", process=b, trigger=OnEvent("Ping")),
        # A start root so neither A nor B is *also* reported unreachable.
        Activation(id="seed", process=_emit_process("seed", "Pong"), trigger=OnStart()),
    )
    result = lint_playbook(pb)
    assert "unguarded-cycle" in result.codes()


def test_guarded_cycle_is_not_flagged() -> None:
    # Same cycle but B's OnEvent carries a 'where' guard -> not a runaway.
    a = Process(
        name="a",
        roles=(_role(),),
        steps=(Step(id="s", by="author", at="day 0", emits=(EmittedEvent("Ping"),)),),
        declares=Declares(events=("Ping",)),
    )
    b = Process(
        name="b",
        roles=(_role(),),
        steps=(Step(id="s", by="author", at="day 0", emits=(EmittedEvent("Pong"),)),),
        declares=Declares(events=("Pong",)),
    )
    pb = _playbook(
        Activation(id="A", process=a, trigger=OnEvent("Pong")),
        Activation(
            id="B", process=b, trigger=OnEvent("Ping", where=(Match("payload.go", "eq", 1),))
        ),
        Activation(id="seed", process=_emit_process("seed", "Pong"), trigger=OnStart()),
    )
    assert "unguarded-cycle" not in lint_playbook(pb).codes()


# --------------------------------------------------------------------------- #
# 5. Feasibility & volume (cost linter).
# --------------------------------------------------------------------------- #


def test_volume_explosion_is_warned() -> None:
    pb = _playbook(
        Activation(
            id="flood", process=_emit_process(), trigger=Probabilistic(rate=100.0, per="day")
        ),
    )
    result = lint_playbook(pb)
    assert "volume-explosion" in result.codes()
    assert result.ok  # a volume warning does not fail the lint


def test_modest_probabilistic_rate_is_fine() -> None:
    pb = _playbook(
        Activation(id="ok", process=_emit_process(), trigger=Probabilistic(rate=0.5, per="week")),
    )
    assert "volume-explosion" not in lint_playbook(pb).codes()


def test_unmet_deliverable_expectation_is_warned() -> None:
    pb = Playbook(
        name="pb",
        vertical="technology",
        activations=(Activation(id="a", process=_emit_process(), trigger=OnStart()),),
        deliverable_expectations=("phantom_doc",),
    )
    assert "unmet-expectation" in lint_playbook(pb).codes()


# --------------------------------------------------------------------------- #
# 6. Determinism AST rule (D23).
# --------------------------------------------------------------------------- #


def test_scan_flags_wall_clock() -> None:
    src = "import datetime\ndef f():\n    return datetime.datetime.now()\n"
    diags = scan_impl_source(src, location="impl")
    assert any(d.code == "nondeterminism" and d.severity is Severity.ERROR for d in diags)


def test_scan_flags_unseeded_random() -> None:
    src = "import random\ndef f():\n    return random.random()\n"
    diags = scan_impl_source(src)
    assert any(d.code == "nondeterminism" for d in diags)


def test_scan_flags_uuid_and_secrets() -> None:
    uuid_src = "import uuid\nx = uuid.uuid4()"
    secrets_src = "import secrets\nx = secrets.token_hex()"
    assert any(d.code == "nondeterminism" for d in scan_impl_source(uuid_src))
    assert any(d.code == "nondeterminism" for d in scan_impl_source(secrets_src))


def test_scan_allows_seeded_random_instance() -> None:
    src = "import random\ndef f(seed):\n    rng = random.Random(seed)\n    return rng.random()\n"
    assert scan_impl_source(src) == []


def test_scan_reports_syntax_error() -> None:
    diags = scan_impl_source("def f(:\n    pass")
    assert any(d.code == "impl-syntax-error" for d in diags)


def test_impl_determinism_flagged_via_resolved_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Write a real module with a wall-clock read, put it on sys.path, and point an
    # impl process at it: lint_process should resolve + scan it and flag it.
    import sys

    pkg = tmp_path / "badimpl"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "proc.py").write_text(
        "import datetime\n\n\nclass Proc:\n"
        "    def run(self):\n        return datetime.datetime.now()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    # Ensure a fresh import lookup.
    sys.modules.pop("badimpl", None)
    sys.modules.pop("badimpl.proc", None)

    proc = Process(
        name="p",
        roles=(_role(),),
        impl="badimpl.proc:Proc",
        declares=Declares(events=("E",)),
    )
    assert "nondeterminism" in lint_process(proc).codes()
