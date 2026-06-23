"""Tests for the Tier-2 test kit machinery (ARCHITECTURE §13, esim-bb00bb20).

Covers the auto-synthesised world (``TestWorld.satisfying`` / ``for_playbook``), the
isolated runners (``run_process`` / ``run_playbook``), the fluent result queries, and
the golden-snapshot helper — the plumbing the conformance suite sits on top of.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from enterprise_sim.authoring import sdk
from enterprise_sim.authoring import testkit as tk
from enterprise_sim.authoring.patterns import build_software, run_clinical_study, sell_merchandise


def _design_review() -> sdk.Process:
    """The declarative design-review process from the build_software reference."""
    return next(
        a.process for a in build_software().activations if a.process.name == "design_review"
    )


# --------------------------------------------------------------------------- #
# TestWorld synthesis.
# --------------------------------------------------------------------------- #


def test_satisfying_binds_every_role() -> None:
    process = _design_review()
    tw = tk.TestWorld.satisfying(process)
    # A candidate pool exists for each selector-backed role.
    leads = tw.world.nodes_by_type("Person")
    assert leads, "expected synthesised Person candidates"
    # The anchor was materialised.
    assert tw.anchor is not None and tw.world.get_node(tw.anchor) is not None


def test_satisfying_candidates_satisfy_where_filters() -> None:
    process = _design_review()
    tw = tk.TestWorld.satisfying(process)
    reviewers_sel = next(r.select for r in process.roles if r.name == "reviewers")
    assert reviewers_sel is not None
    matching = [n for n in tw.world.nodes_by_type("Person") if n.props.get("team") == "engineering"]
    # Enough engineering reviewers to satisfy the 2..3 count.
    assert len(matching) >= 3


def test_satisfying_external_selector_makes_one_party() -> None:
    pb = sell_merchandise()
    negotiate = next(a.process for a in pb.activations if a.process.name == "supplier_negotiation")
    tw = tk.TestWorld.satisfying(negotiate)
    # The external supplier role synthesises a singleton party, not a multi-node pool.
    assert tw.world.get_node("supplier:0") is not None
    assert tw.world.get_node("supplier:1") is None


def test_satisfying_precreates_mutate_targets() -> None:
    pb = run_clinical_study()
    author = next(a.process for a in pb.activations if a.process.name == "author_protocol")
    tw = tk.TestWorld.satisfying(author)
    # author_protocol mutates study:trial7.stage — the node must pre-exist for I7.
    assert tw.world.get_node("study:trial7") is not None


def test_for_playbook_materialises_bound_ids_and_anchors() -> None:
    tw = tk.TestWorld.for_playbook(sell_merchandise())
    assert tw.world.get_node("supplier:acme") is not None  # explicit bind
    assert tw.world.get_node("sku:widget") is not None  # anchor


# --------------------------------------------------------------------------- #
# run_process / run_playbook.
# --------------------------------------------------------------------------- #


def test_run_process_emits_expected_event_types() -> None:
    res = tk.run_process(_design_review())
    assert res.event_types() == {"DesignDrafted", "ReviewOpened", "CommentPosted", "DesignApproved"}
    assert res.deliverable("design_doc") is not None


def test_run_process_is_deterministic_across_runs() -> None:
    a = tk.run_process(_design_review())
    b = tk.run_process(_design_review())
    assert a.snapshot() == b.snapshot()


def test_run_process_seed_changes_the_stream() -> None:
    a = tk.run_process(_design_review(), seed=1)
    b = tk.run_process(_design_review(), seed=999)
    # Different seeds change comment counts/placement, so the streams differ.
    assert a.snapshot() != b.snapshot()


def test_run_process_respects_window() -> None:
    start = datetime(2026, 3, 2, 9, 0)
    end = datetime(2026, 3, 13, 17, 0)
    res = tk.run_process(_design_review(), start=start, end=end)
    assert all(start <= e.timestamp <= end for e in res.journal)


def test_run_playbook_runs_the_event_cascade() -> None:
    res = tk.run_playbook(run_clinical_study())
    # The gated chain: author → IRB (OnEvent ProtocolApproved) → start (OnEvent IRBApproved).
    assert "ProtocolDrafted" in res.event_types()
    assert "IRBApproved" in res.event_types()
    assert "StudyStarted" in res.event_types()


def test_run_playbook_is_deterministic() -> None:
    a = tk.run_playbook(run_clinical_study())
    b = tk.run_playbook(run_clinical_study())
    assert a.snapshot() == b.snapshot()


# --------------------------------------------------------------------------- #
# Fluent queries.
# --------------------------------------------------------------------------- #


def test_event_query_count_and_filtering() -> None:
    res = tk.run_process(_design_review())
    comments = res.events("CommentPosted")
    assert comments.count == len(list(comments))
    assert comments.count >= 1
    # Reviewers are the only actors who post comments.
    assert comments.actors("reviewers")


def test_event_query_where_matches_payload() -> None:
    pb = run_clinical_study()
    ae = next(a.process for a in pb.activations if a.process.name == "adverse_event_report")
    res = tk.run_process(ae)
    serious = res.events("AdverseEventReported").where(severity="serious")
    assert serious.count >= 1


def test_deliverable_lookup_returns_event_with_actors() -> None:
    res = tk.run_process(_design_review())
    doc = res.deliverable("design_doc")
    assert doc is not None
    assert doc.actors.get("lead")  # the lead drafts the design doc


# --------------------------------------------------------------------------- #
# Golden snapshots.
# --------------------------------------------------------------------------- #


def test_assert_golden_writes_then_matches(tmp_path: Path) -> None:
    res = tk.run_process(_design_review())
    golden = tmp_path / "design_review.jsonl"
    # First call records (file absent) ...
    tk.assert_golden(res, golden)
    assert golden.exists()
    # ... second call compares against the recorded snapshot and passes.
    tk.assert_golden(tk.run_process(_design_review()), golden)


def test_assert_golden_detects_drift(tmp_path: Path) -> None:
    golden = tmp_path / "g.jsonl"
    tk.assert_golden(tk.run_process(_design_review()), golden)
    # A different seed yields a different stream → golden mismatch.
    drifted = tk.run_process(_design_review(), seed=42)
    try:
        tk.assert_golden(drifted, golden)
    except AssertionError as exc:
        assert "golden mismatch" in str(exc)
    else:  # pragma: no cover - the drift must be detected
        raise AssertionError("expected a golden mismatch")
