"""Per-playbook unit tests + golden snapshots for the reference patterns (§13).

Acceptance criterion of esim-bb00bb20: the conformance suite runs on a sample process
and the golden snapshot is stable. These tests run each reference pattern through the
Tier-2 kit, assert the built-in suite is clean (modulo the known esim-xk7 gap), and
pin the seeded event stream against a committed golden file so any regression in the
scheduler, resolver, or lowering surfaces as a diff.

Regenerate the golden files (after an intended change) with::

    ESIM_UPDATE_GOLDEN=1 pytest tests/playbooks
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from enterprise_sim.authoring import sdk
from enterprise_sim.authoring import testkit as tk
from enterprise_sim.authoring.patterns import build_software, run_clinical_study, sell_merchandise

GOLDEN_DIR = Path(__file__).parent / "golden"


def _process(playbook: sdk.Playbook, name: str) -> sdk.Process:
    return next(a.process for a in playbook.activations if a.process.name == name)


# --------------------------------------------------------------------------- #
# Per-process conformance — every declarative reference process passes the suite.
# --------------------------------------------------------------------------- #

_DECLARATIVE_PROCESSES = [
    ("build_software", "sprint_planning"),
    ("build_software", "design_review"),
    ("build_software", "ship_retro"),
    ("run_clinical_study", "author_protocol"),
    ("run_clinical_study", "irb_review"),
    ("run_clinical_study", "start_study"),
    ("run_clinical_study", "adverse_event_report"),
    ("sell_merchandise", "supplier_negotiation"),
]

_BUILDERS = {
    "build_software": build_software,
    "run_clinical_study": run_clinical_study,
    "sell_merchandise": sell_merchandise,
}


@pytest.mark.parametrize(("playbook_name", "process_name"), _DECLARATIVE_PROCESSES)
def test_declarative_reference_process_conforms(playbook_name: str, process_name: str) -> None:
    process = _process(_BUILDERS[playbook_name](), process_name)
    tk.assert_conforms(tk.run_process(process))


# --------------------------------------------------------------------------- #
# Golden snapshots — seeded streams are stable across runs.
# --------------------------------------------------------------------------- #


def test_design_review_golden() -> None:
    res = tk.run_process(_process(build_software(), "design_review"))
    tk.assert_golden(res, GOLDEN_DIR / "design_review.jsonl")


def test_adverse_event_golden() -> None:
    res = tk.run_process(_process(run_clinical_study(), "adverse_event_report"))
    tk.assert_golden(res, GOLDEN_DIR / "adverse_event_report.jsonl")


def test_clinical_study_playbook_golden() -> None:
    res = tk.run_playbook(run_clinical_study())
    tk.assert_golden(res, GOLDEN_DIR / "run_clinical_study.jsonl")


# --------------------------------------------------------------------------- #
# Determinism — the I6 guarantee, exercised end-to-end on the playbooks.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("builder", [build_software, run_clinical_study, sell_merchandise])
def test_playbook_runs_are_deterministic(builder: Callable[[], sdk.Playbook]) -> None:
    a = tk.run_playbook(builder())
    b = tk.run_playbook(builder())
    assert a.snapshot() == b.snapshot()
