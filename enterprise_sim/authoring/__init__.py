"""Playbook/process authoring SDK plus the quality stack.

The declarative Python authoring surface (ARCHITECTURE §12, decision D21) lives
in :mod:`enterprise_sim.authoring.sdk`: the §12.2 building blocks and the six
triggers, each round-trippable via ``to_dict`` / ``from_dict``. The three
cross-vertical reference playbooks (§12.3) are in
:mod:`enterprise_sim.authoring.patterns`. The Tier-2 isolated test kit and
conformance suite (§13) live in :mod:`enterprise_sim.authoring.testkit`, lowering
the SDK to the engine spec via :mod:`enterprise_sim.authoring.lowering`. Tier 1
(static lint) is :mod:`enterprise_sim.authoring.lint` and Tier 3 (structural +
LLM-judge evaluators) is :mod:`enterprise_sim.authoring.eval`.
"""

from __future__ import annotations

from enterprise_sim.authoring.eval import (
    EvalReport,
    JudgeVerdict,
    Metric,
    Thresholds,
    evaluate,
    format_report,
    judge_sample,
)
from enterprise_sim.authoring.lint import (
    Diagnostic,
    LintResult,
    Severity,
    lint_playbook,
    lint_process,
    scan_impl_source,
)
from enterprise_sim.authoring.patterns import (
    REFERENCE_PLAYBOOKS,
    build_software,
    run_clinical_study,
    sell_merchandise,
)
from enterprise_sim.authoring.sdk import (
    Activation,
    ConditionExpr,
    Declares,
    Deliverable,
    EffectKind,
    EmittedEvent,
    KGEffect,
    Match,
    MatchOp,
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
    Trigger,
    trigger_from_dict,
)
from enterprise_sim.authoring.testkit import (
    ConformanceViolation,
    RunResult,
    TestWorld,
    assert_conforms,
    assert_golden,
    check_conformance,
    check_playbook,
    run_playbook,
    run_process,
    scan_nondeterminism,
    snapshot,
)

__all__ = [
    "REFERENCE_PLAYBOOKS",
    "Activation",
    "ConditionExpr",
    "ConformanceViolation",
    "Declares",
    "Deliverable",
    "Diagnostic",
    "EffectKind",
    "EmittedEvent",
    "EvalReport",
    "JudgeVerdict",
    "KGEffect",
    "LintResult",
    "Match",
    "MatchOp",
    "Metric",
    "OnCadence",
    "OnCondition",
    "OnEvent",
    "OnMilestone",
    "OnStart",
    "Playbook",
    "Probabilistic",
    "Process",
    "Role",
    "RunResult",
    "Selector",
    "Severity",
    "Spread",
    "Step",
    "TestWorld",
    "Thresholds",
    "Trigger",
    "assert_conforms",
    "assert_golden",
    "build_software",
    "check_conformance",
    "check_playbook",
    "evaluate",
    "format_report",
    "judge_sample",
    "lint_playbook",
    "lint_process",
    "run_clinical_study",
    "run_playbook",
    "run_process",
    "scan_impl_source",
    "scan_nondeterminism",
    "sell_merchandise",
    "snapshot",
    "trigger_from_dict",
]
