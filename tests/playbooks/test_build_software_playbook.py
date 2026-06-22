"""Validation suite for the full ``build_software`` plugin (esim-3b54f392, §12.3).

Acceptance for M5: the authored ``build_software`` playbook + its four processes
(``project_kickoff``, ``sprint_cycle``, ``weekly_status``, ``design_review``) pass
the whole quality stack — Tier 1 lint, the Tier 2 conformance suite (I1–I8) and
playbook invariants (P1–P6), bespoke domain assertions, a committed golden
snapshot, and Tier 3 structural evaluation — all green, with the engine core
untouched.

Regenerate the golden file (after an intended change) with::

    ESIM_UPDATE_GOLDEN=1 pytest tests/playbooks/test_build_software_playbook.py
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from enterprise_sim.authoring import sdk
from enterprise_sim.authoring import testkit as tk
from enterprise_sim.authoring.eval import Thresholds, evaluate
from enterprise_sim.authoring.lint import lint_playbook, lint_process
from enterprise_sim.core.registry import PLAYBOOKS, PROCESSES, discover_all
from enterprise_sim.playbooks.build_software import (
    BUILD_SOFTWARE,
    build_software,
    design_review,
    project_kickoff,
    sprint_cycle,
    weekly_status,
)

GOLDEN_DIR = Path(__file__).parent / "golden"

_PROCESS_FACTORIES: dict[str, Callable[[], sdk.Process]] = {
    "project_kickoff": project_kickoff,
    "sprint_cycle": sprint_cycle,
    "weekly_status": weekly_status,
    "design_review": design_review,
}


# --------------------------------------------------------------------------- #
# Tier 1 — static lint is clean for the playbook and every process.
# --------------------------------------------------------------------------- #


def test_playbook_lints_clean() -> None:
    result = lint_playbook(build_software())
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.diagnostics == (), [str(d) for d in result.diagnostics]


@pytest.mark.parametrize("name", sorted(_PROCESS_FACTORIES))
def test_each_process_lints_clean(name: str) -> None:
    result = lint_process(_PROCESS_FACTORIES[name]())
    assert result.ok, [str(d) for d in result.diagnostics]
    assert result.diagnostics == (), [str(d) for d in result.diagnostics]


# --------------------------------------------------------------------------- #
# Tier 2 — per-process conformance (I1–I8) and the playbook invariants (P1–P6).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(_PROCESS_FACTORIES))
def test_each_process_conforms(name: str) -> None:
    tk.assert_conforms(tk.run_process(_PROCESS_FACTORIES[name]()))


def test_playbook_is_p_clean() -> None:
    # Unlike the minimal teaching reference, the real plugin has no dead trigger,
    # no unreachable activation, and no unguarded cycle.
    assert tk.check_playbook(build_software()) == []


def test_playbook_run_conforms() -> None:
    tk.assert_conforms(tk.run_playbook(build_software()))


# --------------------------------------------------------------------------- #
# Custom domain assertions — the corpus has the shape a product team produces.
# --------------------------------------------------------------------------- #


def test_triggering_graph_drives_every_process() -> None:
    res = tk.run_playbook(build_software())
    types = res.event_types()
    # Kickoff (OnStart), cadence sprints + weekly status, and the event-triggered review.
    assert {"ProjectKickedOff", "SprintPlanned", "StatusReported", "DesignDrafted"} <= types
    # Each sprint plan fans out to exactly one design review draft.
    sprints = res.events("SprintPlanned").count
    assert res.events("DesignDrafted").count == sprints
    # Approvals lag a multi-day review, so the final sprint's review may not close
    # inside the window — every earlier one does.
    assert sprints - 1 <= res.events("DesignApproved").count <= sprints


def test_expected_deliverables_are_all_produced() -> None:
    res = tk.run_playbook(build_software())
    assert set(build_software().deliverable_expectations) <= res.deliverable_kinds()


def test_review_threads_are_multi_reviewer() -> None:
    res = tk.run_playbook(build_software())
    comments = res.events("CommentPosted")
    assert comments.count > 0
    # Comments are spread across more than one reviewer (not dumped by one person).
    assert len(comments.actors("reviewers")) >= 2


def test_milestones_are_announced() -> None:
    res = tk.run_playbook(build_software())
    assert res.has_milestone("project_kicked_off")
    assert res.has_milestone("design_signed_off")


def test_cadence_recurs_across_the_window() -> None:
    res = tk.run_playbook(build_software())
    # A 12-week window yields multiple sprints and ~weekly status reports.
    assert res.events("SprintPlanned").count >= 4
    assert res.events("StatusReported").count >= 8


# --------------------------------------------------------------------------- #
# Tier 2 — golden snapshot + determinism.
# --------------------------------------------------------------------------- #


def test_playbook_golden() -> None:
    res = tk.run_playbook(build_software())
    tk.assert_golden(res, GOLDEN_DIR / "build_software.jsonl")


def test_design_review_golden() -> None:
    res = tk.run_process(design_review())
    tk.assert_golden(res, GOLDEN_DIR / "build_software_design_review.jsonl")


def test_run_is_deterministic() -> None:
    a = tk.run_playbook(build_software())
    b = tk.run_playbook(build_software())
    assert a.snapshot() == b.snapshot()


# --------------------------------------------------------------------------- #
# Tier 3 — structural realism evaluation passes on the produced corpus.
# --------------------------------------------------------------------------- #


def test_tier3_structural_eval_is_green() -> None:
    res = tk.run_playbook(build_software())
    report = evaluate(res.journal)
    assert report.ok, [str(m) for m in report.failures()]


def test_tier3_eval_holds_under_stricter_thresholds() -> None:
    # The corpus is comfortably realistic — it survives tightened thresholds too,
    # so the green result is not just the lenient defaults squeaking by.
    res = tk.run_playbook(build_software())
    strict = Thresholds(
        comments_per_reviewer=0.5,
        working_hours_adherence=1.0,
        cadence_plausibility=0.9,
        role_participation_balance=0.5,
    )
    report = evaluate(res.journal, thresholds=strict, comment_types={"CommentPosted"})
    assert report.ok, [str(m) for m in report.failures()]


# --------------------------------------------------------------------------- #
# Registration — the plugin self-registers into the process-wide catalogs.
# --------------------------------------------------------------------------- #


def test_plugin_is_discoverable() -> None:
    discover_all()  # idempotent; import side effects already fired
    assert "build_software" in PLAYBOOKS
    assert PLAYBOOKS.get("build_software").vertical == "technology"
    # The registered plugin rebuilds a fresh, correctly-named playbook on demand.
    assert BUILD_SOFTWARE.build().name == "build_software"
    for name in _PROCESS_FACTORIES:
        assert name in PROCESSES


def test_engineering_archetype_playbook_is_registered() -> None:
    # The engineering archetype declares it runs ``build_software``; that name now
    # resolves to a real registered plugin, not just a reference pattern.
    assert "build_software" in PLAYBOOKS


# --------------------------------------------------------------------------- #
# Round-trip — the authoring tree serializes and reconstructs identically (§12).
# --------------------------------------------------------------------------- #


def test_playbook_round_trips() -> None:
    playbook = build_software()
    assert sdk.Playbook.from_dict(playbook.to_dict()) == playbook
