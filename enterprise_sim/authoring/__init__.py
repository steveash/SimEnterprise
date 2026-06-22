"""Playbook/process authoring SDK plus the quality stack.

The declarative Python authoring surface (ARCHITECTURE §12, decision D21) lives
in :mod:`enterprise_sim.authoring.sdk`: the §12.2 building blocks and the six
triggers, each round-trippable via ``to_dict`` / ``from_dict``. The three
cross-vertical reference playbooks (§12.3) are in
:mod:`enterprise_sim.authoring.patterns`. The lint / test-kit / eval tiers (§13)
land alongside in later milestones.
"""

from __future__ import annotations

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

__all__ = [
    "REFERENCE_PLAYBOOKS",
    "Activation",
    "ConditionExpr",
    "Declares",
    "Deliverable",
    "Diagnostic",
    "EffectKind",
    "EmittedEvent",
    "KGEffect",
    "LintResult",
    "Match",
    "MatchOp",
    "OnCadence",
    "OnCondition",
    "OnEvent",
    "OnMilestone",
    "OnStart",
    "Playbook",
    "Probabilistic",
    "Process",
    "Role",
    "Selector",
    "Severity",
    "Spread",
    "Step",
    "Trigger",
    "build_software",
    "lint_playbook",
    "lint_process",
    "run_clinical_study",
    "scan_impl_source",
    "sell_merchandise",
    "trigger_from_dict",
]
